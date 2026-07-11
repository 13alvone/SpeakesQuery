"""
Log Writer
──────────
Thread-safe buffered log-row emitter that writes Parquet files into
``indexes/logs/<category>/`` with its own size budget (independent of the
main ``indexes/`` budget).

Design goals
  * Cheap to call from hot paths - rows are appended to an in-memory deque
    under a category-scoped lock, never block on disk IO.
  * Periodic background flush (daemon thread) plus hard flush thresholds
    (``_MAX_BUFFERED_ROWS``) so a crash loses at most one interval of logs.
  * Reuses ``ParquetWriter.write_atomic`` for the actual durability story:
    gzip compression, tmp-then-rename, ``_epoch`` column required.
  * Cleanup is the caller's problem - the logs-root budget is enforced by
    ``cleanup_logs()`` in ``scheduled_input_engine.cleanup``, scheduled on
    the same APScheduler interval as the main indexes cleanup.

Categories and schemas are declared up-front so each log file is uniform
Parquet (readable via SPQL: ``index="indexes/logs/claude_api/*.parquet"``).
Unknown columns in a row are silently dropped; known columns missing from a
row land as ``None``.

The module exposes a singleton ``LogWriter`` plus convenience functions
(``log_claude_api_call`` etc.) for the common producers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from scheduled_input_engine.parquet_writer import ParquetWriter

logger = logging.getLogger(__name__)

# ── Category schemas ──────────────────────────────────────────────

# Every category's rows are projected to exactly these columns, in this
# order, before being written. Missing columns become None; extras dropped.
#
# _epoch is injected automatically at write() time if not provided.
SCHEMAS: dict[str, list[str]] = {
    "config": [
        "_epoch", "action", "subject", "subject_type",
        "old_value", "new_value", "actor", "source",
    ],
    "search_runs": [
        "_epoch", "search_name", "status", "row_count",
        "duration_ms", "error_message", "query_hash", "triggered_by",
    ],
    "alert_groups": [
        "_epoch", "group_name", "status", "searches_used",
        "estimated_tokens", "actual_tokens", "cost_usd",
        "error_message", "duration_ms", "dry_run",
        # Per-phase timings added 2026-04-21 so operators can SPQL-
        # aggregate "which phase is slowest" across many dispatches.
        # Previously these were only in stdout logs and un-queryable.
        "feeder_loop_ms", "claude_call_ms", "email_send_ms",
    ],
    "claude_api": [
        "_epoch", "request_id", "group_name", "source", "model",
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_creation_tokens", "cost_usd", "latency_ms", "status",
        "error_class", "error_message", "stop_reason",
        "attempt_num", "retried",
        # Headroom routing path for this attempt (2026-06-23):
        # "headroom" | "direct" | "direct-fallback". Lets us measure
        # compression savings once the proxy enables compression, and
        # spot fail-open events. Additive; older rows read NULL.
        "headroom_path",
    ],
    "ingestion": [
        "_epoch", "task_id", "title", "status", "duration_ms",
        "error_message", "row_count", "attempt", "trust_level",
    ],
    # Phase 4 / Bet 4 slice 8a (2026-05-09) - failed-feeder patch
    # drafter. One row PER PATCH SUGGESTION emitted by the
    # ``analyzers.patch_drafter`` module, written from the engine's
    # failure-path wiring AND from the on-demand
    # ``/api/patch-drafter/suggest`` endpoint. Operators read these
    # rows via SPQL (``index="indexes/logs/patch_suggestions/*"``) to
    # review the suggested diff + apply it manually. Slice 8b will
    # add GitHub PR creation on top of these rows. Schema is
    # ADDITIVE-ONLY going forward - the operator's audit history of
    # suggested fixes survives indefinitely.
    "patch_suggestions": [
        "_epoch",            # suggestion event Unix seconds
        "task_id",           # ingestion task id (or "" for ad-hoc on-demand)
        "title",             # ingestion script title
        "error_hash",        # 16-hex stable hash of the error_message (dedup key)
        "status",            # success | dry_run | skipped_budget | skipped_no_key | error
        "model",             # Claude model id used
        "cost_usd",          # actual cost (success) or worst-case estimate (dry_run)
        "latency_ms",        # network call duration
        "patch",             # unified diff text (may be empty if NO_CONFIDENT_FIX)
        "explanation",       # plain-English explanation
        "request_id",        # joins to claude_api_history
        "error_message",     # the original ingestion error_message (truncated)
        "input_tokens",      # actual (success) or worst-case (dry_run) input tokens
        "output_tokens",     # actual (success) or worst-case (dry_run) output tokens
        "drafter_error_class",   # only populated on status=error|skipped_*
        "drafter_error_message", # only populated on status=error|skipped_*
    ],
    "system": [
        "_epoch", "level", "component", "event", "message",
    ],
    # Alert-group pick capture - one row per opportunity suggested in a
    # dispatch. Populated by the dispatcher after Claude returns a
    # fenced JSON block. Queryable via
    # ``index="indexes/IMMUTABLE/ag_picks/*.parquet"`` for backtesting,
    # alerting, and dedup (prior 24h → reserved-picks feeder).
    # Added 2026-04-21.
    "ag_picks": [
        "_epoch",                  # dispatch unix seconds (indexing anchor)
        "event_timestamp",         # ISO UTC of dispatch (human readable)
        "alert_group",             # "daily_opportunity_brief" / "global_macro_risk_brief" / future AGs
        "run_request_id",          # Claude request_id - joins to claude_api_history
        "rank_in_brief",           # 1..7 (1-5 TOP; 6-7 HONORABLE_MENTION). Accepts `pick_rank` from Claude JSON.
        "pick_tier",               # "TOP" | "HONORABLE_MENTION". Added 2026-04-23 with new prompt template.
        "idea_id",                 # canonical "{type}:{id}:{direction}" lowercased
        "instrument_type",         # polymarket | kalshi | equity | crypto | option | commodity | forex | etf
        "instrument_id",           # slug/ticker/symbol
        "direction",               # YES | NO | LONG | SHORT | BUY | SELL
        "conviction_pct",          # int 75-100
        "expected_return_pct",     # float (Claude's estimate)
        "position_size_tier",      # SMALL | MEDIUM | LARGE
        "entry_price",             # float - target entry
        "suggested_buy_epoch",     # int - WHEN to enter
        "suggested_sell_epoch",    # int - WHEN to exit
        "hold_hours",              # int - (sell - buy) / 3600, redundant but SPQL-friendly
        "take_profit_price",       # float|None - win threshold
        "stop_loss_price",         # float|None - loss threshold
        "exit_catalyst",           # free text - human + downstream-AI-tool friendly
        "thesis",                  # free text - ≤2 sentences
        "source_signals",          # semicolon-joined feeder names
        "correlation_cluster",     # short label grouping correlated picks within a brief (e.g. "ai_infra_long", "idiosyncratic")
        "short_squeeze_risk_json",  # JSON string {short_interest_pct, days_to_cover, borrow_assessment} for shorts; null for non-shorts
        "status",                  # "open" at write; later "won"/"lost"/"time_exit"
        # Wave 3 (2026-04-25): provenance columns for manual-return loop.
        # `source` distinguishes Claude-pipeline picks ("claude") from
        # operator-pasted external-LLM picks ("manual"). `model_used`
        # carries the model id (e.g. "claude-sonnet-4-6", "gpt-4o",
        # "gemini-2.5-pro"), so historical-performance queries can group
        # by model. Old rows read NULL for both - no migration needed.
        "source",                  # "claude" | "manual"
        "model_used",              # model id string
        # Options-pick fields (Wave 1 of Options Edge Brief, 2026-04-26).
        # All optional - non-options AGs leave these NULL. The OEB prompt
        # instructs Claude to populate them; the dispatcher forwards them
        # through ``_validate_and_normalize_pick`` → ``_log_picks`` → here.
        # Wave 2 reads these columns to mark-to-market the pick journal.
        "option_structure",        # "long_call" | "long_put" | "vertical_debit_spread" | "vertical_credit_spread" | "iron_condor" | "calendar" | "straddle" | "strangle" | "covered_call" | "cash_secured_put"
        "option_legs_json",        # JSON array of legs [{"action","right","strike","expiration","qty","limit","contract_symbol"}, ...]
        "option_max_loss_usd",     # max dollar risk per 1-contract position (positive)
        "option_max_profit_usd",   # max dollar profit per 1-contract position (positive; NULL for unlimited e.g. long calls)
        "option_net_debit_credit",  # positive = net debit paid, negative = net credit received
        "option_dte_days",         # days to expiration of the longest-DTE leg
        "option_difficulty_tier",  # "BEGINNER" | "INTERMEDIATE" | "ADVANCED" - drives the three-tier learner format
        "account_size_floor_usd",  # minimum account size where this pick fits at <=2% sizing; flags small accounts
    ],
    # ── Wave 2 of Options Edge Brief (2026-04-26): closure events ───
    # One row PER PICK CLOSURE (won / lost / time_exit / expired).
    # Joins to ``ag_picks`` rows on ``idea_id`` to produce the full
    # entry + exit ledger. Schema is ADDITIVE-ONLY for the decade-
    # horizon trading record; never remove a column. Lives in
    # ``indexes/IMMUTABLE/ag_picks_closures/*.parquet`` (protected from
    # cleanup). Written by the ``oeb_pick_tracker_pro`` ingestion script
    # via ``log_ag_pick_closure(...)`` once per pick that hits an exit
    # rule.
    "ag_picks_closures": [
        "_epoch",                  # closure event timestamp (Unix s)
        "event_timestamp",         # ISO UTC of closure (human readable)
        "alert_group",             # joins to original entry row
        "idea_id",                 # joins to original entry row
        "instrument_type",         # always "option" for OEB; carried for cross-AG flexibility
        "instrument_id",           # carried from entry
        "outcome",                 # "won" | "lost" | "time_exit" | "expired"
        "trigger_rule",            # "stop_loss_hit" | "take_profit_hit" | "time_stop" | "expiration"
        "entry_price",             # carried from entry
        "exit_price",              # final net debit/credit at close (signed same as entry_price)
        "exit_epoch",              # Unix seconds - when the rule fired
        "pnl_per_contract_usd",    # signed, per 1 contract; positive = win for long premium etc.
        "pnl_pct_vs_max_loss",     # +100 = full max profit; -100 = full max loss; 0 = breakeven
        "days_held",               # round((exit - entry) / 86400, 2)
        "leg_prices_at_close_json",  # JSON array of {contract_symbol, mid, bid, ask}
        "closure_quality",         # "clean" | "gap_through_stop" | "illiquid" | "expired_otm" | "expired_itm"
        "account_size_floor_usd",  # carried from entry - for dual hit-rate compute
        "fits_account_at_entry",   # bool - was this pick within current_account_size_usd at entry?
        "current_account_size_usd_at_close",  # the user's account size setting at close time
        "fits_account_at_close",   # bool - does the pick still fit (account may have grown)?
    ],
    # ── Wave 2 of Options Edge Brief (2026-04-26): weekly review obs ─
    # One row per OBSERVATION emitted by the options_performance_review
    # alert group. The review AG dispatches Claude over the past 7d +
    # 30d closures, asks for structured findings (hit rates, signal-
    # class winners/losers, ONE rule-tweak recommendation), and the
    # dispatcher writes each observation here. Lives in
    # ``indexes/IMMUTABLE/ag_picks_review_observations/*.parquet``.
    # Schema additive-only.
    "ag_picks_review_observations": [
        "_epoch",                  # observation event timestamp (Unix s)
        "event_timestamp",         # ISO UTC of observation
        "alert_group",             # source AG name (always "options_performance_review")
        "run_request_id",          # Claude request_id; joins to claude_api_history
        "review_period_start",     # ISO date - start of analyzed window
        "review_period_end",       # ISO date - end of analyzed window
        "review_period_days",      # int - typically 7 or 30
        "n_picks_overall",         # int - picks observed in the window
        "n_picks_account_fit",     # int - picks that fit current_account_size_usd
        "hit_rate_overall",        # 0.0 - 1.0
        "hit_rate_account_fit",    # 0.0 - 1.0
        "best_signal_class",       # e.g. "iv_rank_high"
        "worst_signal_class",      # e.g. "earnings_implied_move"
        "observation_text",        # free text - single observation
        "observation_evidence",    # citation / numbers
        "observation_actionable",  # bool - should this drive a rule tweak?
        "rule_tweak_recommendation_text",  # the ONE recommended tweak (only on the summary row, NULL on observation rows)
        "rule_tweak_rationale",    # why
        "rule_tweak_expected_impact",  # what the user should expect
        "row_kind",                # "summary" | "observation" - discriminator within a single review
        # Calibration verdict (added 2026-05-06 - Bucket 1.5 follow-on of
        # the OEB attribution prompt edits). Captures whether the
        # analyst's conviction_pct ratings predicted outcomes. NULL on
        # rows from review runs that pre-date the calibration prompt
        # edit, or where the sample was too small (< 10 closures over 30
        # days) to render a verdict. Per-bucket detail lives in the
        # markdown email body for now; bucket-level columns can be added
        # additively later if useful.
        "calibration_status",      # "well_calibrated" | "overconfident" | "underconfident" | "insufficient_data" | ""
        "calibration_n_closures",  # int - closures used in the calibration analysis (0 if not computed)
    ],
    # ── Curator / speaktube integration (Phase 6 / Bet 5 slice 1, 2026-05-16) ─
    # One row per playback event the speaktube player streams to its
    # local JSONL log, ingested hourly by the curator_telemetry_pull
    # script. Lives in ``indexes/IMMUTABLE/curator_telemetry/*.parquet``
    # because the long-tail goal is the user owning a decade of their
    # own viewing telemetry. Schema is ADDITIVE-ONLY going forward;
    # forward-compat handled via ``raw_json`` which carries the full
    # original event (so a renderer-side schema extension on speaktube's
    # side never breaks ingest).
    "curator_telemetry": [
        "_epoch",                  # event timestamp (Unix s)
        "event_ts_iso",            # raw ISO 8601 from the player (includes local TZ offset)
        "event_date",              # ISO date in player's local TZ - partition key for daily dignity score
        "event_type",              # play_start | play_end | skip | rate | mark_junk | reflection_submit | manual_search | impression | <future>
        "video_external_id",       # platform video id (YouTube 11-char or whatever yt-dlp recognises); "" for events without a video (e.g. manual_search)
        "chosen_by",               # curator | user_manual | recommendation | playlist | ""
        "run_date",                # ISO date of the playlist the event traces back to ("" for non-curator)
        "position",                # 1-indexed playlist position ("" / null for non-curator)
        "slot_kind",               # main | surprise | movie | <future> ("" / null when not applicable)
        "watched_seconds",         # play_end / skip
        "total_seconds",           # play_end / skip
        "rating",                  # rate (1-9)
        "reason",                  # mark_junk free text
        "kind",                    # reflection_submit kind (eod | per_video) - see also the dedicated reflections store
        "content",                 # reflection_submit content (free text)
        "query",                   # manual_search query
        "raw_json",                # full original event as a JSON string - preserves any extra fields speaktube adds
    ],
    # User-submitted reflection notes (POST /api/reflections + the
    # reflection_submit telemetry stream both land rows here). Free-form
    # text the user wrote about their viewing day or about a single
    # video. Treat as forever-data; the user's stated goal is to
    # accumulate self-observation alongside their viewing record.
    "curator_reflections": [
        "_epoch",                  # write epoch (Unix s)
        "event_ts_iso",            # ISO timestamp of the write
        "date",                    # ISO date the reflection is "about" (the player passes this)
        "kind",                    # eod | per_video | <future>
        "content",                 # free text - the reflection body
        "video_external_id",       # platform video id ("" when kind=eod)
        "source",                  # api_post | telemetry_event - provenance
    ],
    # Speaktube user keyword preferences (Phase 6 / Bet 5 slice 11,
    # 2026-05-17 - req #10). Each POST /api/preferences/keywords
    # writes one row per keyword. The dispatcher's
    # ``_maybe_apply_keyword_boost`` hook reads the "active pool" (rows
    # written since the most-recent ``curator_playlist`` composition
    # OR trailing 24h fallback) at AG-fire time and boosts
    # ``interest_score`` on title-matching candidates. Schema
    # ADDITIVE-ONLY going forward - historical keyword pools tell
    # us how the operator's interests evolved over time. The "expires
    # after next composer fire" semantic from the speaktube spec is
    # functional only; the row itself lives forever.
    "curator_keyword_prefs": [
        "_epoch",                  # write epoch (Unix s)
        "event_ts_iso",            # ISO timestamp of the write
        "keyword",                 # the keyword string (case preserved as posted)
        "source",                  # api_post (today) | <future like speaktube_search_now>
        "raw_request",             # full request body JSON for forensics (truncated 5000)
    ],
    # Composed-playlist output. One row PER ITEM in a composed run; a
    # full playlist of N items writes N rows sharing the same run_date /
    # composed_at_epoch. The /api/playlist/today endpoint reads the
    # most-recent run_date and reconstructs the JSON shape speaktube
    # expects. Schema ADDITIVE-ONLY - historical playlist composition
    # is the substrate for "what did the curator suggest 6 months ago"
    # analytics + post-hoc dignity audits.
    "curator_playlist": [
        "_epoch",                  # composed-at epoch (same for all rows in a run)
        "run_date",                # ISO date the playlist is for (player-local TZ)
        "composed_at_iso",         # ISO UTC of composition
        "growth_dial",             # -1.0..+1.0 bipolar float (slice 8 2026-05-17, was 0.0-1.0) - value of the knob AT compose time
        "theme",                   # free-form daily-vibe tag ("" if no theme)
        "position",                # 1-indexed render order
        "slot_kind",               # main | surprise | movie | <extensible>
        "rationale",               # short user-facing "why this is here" text
        "external_id",             # platform video id
        "url",                     # yt-dlp-resolvable URL (load-bearing for playback)
        "title",                   # display only
        "channel_name",            # display only
        "thumbnail_url",           # display image URL ("" when source feed omits it; slice 4 2026-05-17)
        "published_at",            # ISO 8601 publication date ("" when feed omits it; slice 4 2026-05-17)
        "duration_seconds",        # display only (may be null)
        "interest_score",          # 0.0-1.0 (may be null)
        "growth_score",            # 0.0-1.0 (may be null)
        "slop_score",              # 0.0-1.0 (may be null)
        "score_reasoning",         # free-form text under the "Why this is here" disclosure
        "thin_history_active",     # bool (slice 10, 2026-05-17) - was thin-history aggressive-discovery mode active at compose time?
    ],
    # Curator topic snapshots - Phase 6 / Bet 5 slice 3 (2026-05-16).
    # One row PER CLUSTER per snapshot; a 10-cluster snapshot writes 10
    # rows sharing the same ``snapshot_epoch`` + ``snapshot_id``. Lives
    # in ``indexes/IMMUTABLE/curator_topic_snapshots/*.parquet`` because
    # the topic-evolution timeline ("what was the user into 6 months
    # ago?") is forever-data, exactly like the playlist + telemetry
    # streams. Schema is ADDITIVE-ONLY going forward; same CLAUDE.md
    # rule as the other curator_* + ag_picks_* schemas.
    "curator_topic_snapshots": [
        "_epoch",                  # write epoch (Unix s) - added by emit()
        "snapshot_epoch",          # when the snapshot was computed (load-bearing for "snapshot at time T" queries)
        "snapshot_id",             # UUID, ties every cluster row from one snapshot together
        "model_name",              # embedder model (sentence-transformers/all-MiniLM-L6-v2 etc.)
        "dim",                     # embedding dimension (384 for MiniLM-L6)
        "n_clusters",              # total cluster count in this snapshot (== rows sharing snapshot_id)
        "n_history_rows",          # number of watch-history rows fed into KMeans
        "decay_lambda_days",       # recency-decay half-life in days at compute time
        "cluster_id",              # cluster ordinal within snapshot (0-indexed)
        "centroid_json",           # JSON-encoded list[float] - the L2-normalised cluster centroid vector
        "weight",                  # sum of member recency weights (cluster "importance")
        "n_members",               # raw count of history rows in this cluster
        "exemplar_titles_json",    # JSON-encoded list[str] - top-N titles closest to centroid
        "label",                   # human-readable label (empty if LLM labeling skipped or failed)
    ],
}

VALID_CATEGORIES = frozenset(SCHEMAS.keys())

# Categories that should land in the IMMUTABLE namespace (Wave 2 of
# Options Edge Brief, 2026-04-26) instead of the logs/ tree. These are
# explicitly protected from cleanup - see ``settings.immutable_dir()``
# and the cleanup ``skip_subdirs`` mechanism. Adding a category here
# changes its on-disk path from ``indexes/logs/<cat>/`` to
# ``indexes/IMMUTABLE/<cat>/`` for new writes; old data should be
# physically migrated by a one-shot script if continuity matters.
IMMUTABLE_CATEGORIES: frozenset = frozenset({
    "ag_picks",
    "ag_picks_closures",
    "ag_picks_review_observations",
    # Curator / speaktube - see the schemas above. All three are
    # explicitly forever-data: the user's viewing telemetry, their
    # written reflections, and the historical record of what the
    # curator suggested. None of these should ever be garbage-collected
    # by the cleanup budget.
    "curator_telemetry",
    "curator_reflections",
    "curator_playlist",
    "curator_keyword_prefs",  # slice 11 2026-05-17 (req #10)
    # Slice 3 (2026-05-16) - topic-evolution timeline is forever-data
    # for the same reasons the other curator_* streams are: the user's
    # 10-year horizon needs every snapshot recoverable, including the
    # centroids+exemplars that defined "their style" at each point.
    "curator_topic_snapshots",
})

# Flush thresholds
_MAX_BUFFERED_ROWS = 500       # hard cap per category before forced flush
_DEFAULT_INTERVAL_SEC = 30     # fallback if setting cannot be read

# Parquet file target size for logs (smaller than default - log rows are tiny)
_LOG_FILE_TARGET_MB = 32


def _now_epoch() -> int:
    """Return current time as Unix epoch seconds (consistent with _epoch)."""
    return int(time.time())


def _coerce_scalar(value: Any) -> Any:
    """Serialise nested lists / dicts to string so Parquet can store them."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        try:
            return ",".join(str(x) for x in value)
        except Exception:
            return str(value)
    if isinstance(value, dict):
        try:
            import json
            return json.dumps(value, default=str)
        except Exception:
            return str(value)
    return value


class LogWriter:
    """Singleton log emitter with buffered periodic flush."""

    _instance: "LogWriter | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "LogWriter":
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = LogWriter()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the singleton - tests that override logs_dir need a fresh one."""
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.shutdown()
                except Exception:
                    pass
                cls._instance = None

    def __init__(self) -> None:
        self._buffers: dict[str, deque] = {c: deque() for c in SCHEMAS}
        self._locks: dict[str, threading.Lock] = {
            c: threading.Lock() for c in SCHEMAS
        }
        self._writer: ParquetWriter | None = None
        self._immutable_writer: ParquetWriter | None = None
        self._logs_root: Path | None = None
        self._immutable_root: Path | None = None
        self._interval: int = _DEFAULT_INTERVAL_SEC
        self._enabled: bool = True
        self._shutdown = threading.Event()
        self._flusher: threading.Thread | None = None
        self._load_settings()

    def _load_settings(self) -> None:
        """Refresh logs_root + flush interval + enabled flag from settings."""
        try:
            from global_settings import get_settings
            settings = get_settings()
            self._enabled = bool(settings.get("logs_enabled"))
            self._interval = int(settings.get("logs_flush_interval_seconds") or _DEFAULT_INTERVAL_SEC)
            self._logs_root = settings.logs_dir()
            try:
                self._immutable_root = settings.immutable_dir()
            except Exception:
                # Older settings without immutable_dir(): fall back to
                # default location so IMMUTABLE_CATEGORIES still get a
                # protected home, just at the canonical default path.
                self._immutable_root = (
                    Path(__file__).parent.parent / "indexes" / "IMMUTABLE"
                ).resolve()
        except Exception as exc:
            logger.warning(
                "[!] LogWriter: settings load failed (%s); defaulting to indexes/logs/",
                exc,
            )
            self._logs_root = (
                Path(__file__).parent.parent / "indexes" / "logs"
            ).resolve()
            self._immutable_root = (
                Path(__file__).parent.parent / "indexes" / "IMMUTABLE"
            ).resolve()

    def _ensure_writer(self) -> ParquetWriter | None:
        if not self._enabled:
            return None
        if self._writer is not None:
            return self._writer
        if self._logs_root is None:
            return None
        self._logs_root.mkdir(parents=True, exist_ok=True)
        self._writer = ParquetWriter(
            self._logs_root, target_file_mb=_LOG_FILE_TARGET_MB
        )
        return self._writer

    def _ensure_immutable_writer(self) -> ParquetWriter | None:
        """Get/create the writer rooted at the immutable namespace.

        Wave 2 of Options Edge Brief (2026-04-26). Categories listed in
        :data:`IMMUTABLE_CATEGORIES` route through this writer instead of
        the standard logs writer, so they land in ``indexes/IMMUTABLE/``
        and survive cleanup. Returns ``None`` if logging is disabled or
        the immutable root is unavailable.
        """
        if not self._enabled:
            return None
        if self._immutable_writer is not None:
            return self._immutable_writer
        if self._immutable_root is None:
            return None
        self._immutable_root.mkdir(parents=True, exist_ok=True)
        self._immutable_writer = ParquetWriter(
            self._immutable_root, target_file_mb=_LOG_FILE_TARGET_MB
        )
        return self._immutable_writer

    def _writer_for(self, category: str) -> ParquetWriter | None:
        """Resolve the right writer for a category - IMMUTABLE-routed
        categories get the immutable writer; everything else gets the
        standard logs writer."""
        if category in IMMUTABLE_CATEGORIES:
            return self._ensure_immutable_writer()
        return self._ensure_writer()

    def _ensure_flusher(self) -> None:
        if self._flusher is not None and self._flusher.is_alive():
            return
        t = threading.Thread(
            target=self._flusher_loop,
            name="log-writer-flusher",
            daemon=True,
        )
        self._flusher = t
        t.start()

    def _flusher_loop(self) -> None:
        while not self._shutdown.is_set():
            if self._shutdown.wait(timeout=self._interval):
                break
            try:
                self.flush()
            except Exception as exc:
                logger.warning("[!] LogWriter flusher error: %s", exc)
        # Final flush on shutdown
        try:
            self.flush()
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────

    def emit(self, category: str, row: dict) -> None:
        """Append one log row to the category's buffer.

        Unknown columns are dropped, missing columns land as ``None``,
        ``_epoch`` is auto-filled if absent, nested values are stringified.
        Disabled (``logs_enabled=False``) is a silent no-op so callers can
        emit without guards.
        """
        if not self._enabled:
            return
        if category not in VALID_CATEGORIES:
            logger.warning(
                "[!] LogWriter: unknown category '%s' - dropping row", category,
            )
            return

        schema = SCHEMAS[category]
        projected = {col: _coerce_scalar(row.get(col)) for col in schema}
        if projected.get("_epoch") is None:
            projected["_epoch"] = _now_epoch()

        forced_flush = False
        with self._locks[category]:
            self._buffers[category].append(projected)
            if len(self._buffers[category]) >= _MAX_BUFFERED_ROWS:
                forced_flush = True

        self._ensure_flusher()
        if forced_flush:
            try:
                self._flush_category(category)
            except Exception as exc:
                logger.warning(
                    "[!] LogWriter forced flush failed for %s: %s",
                    category, exc,
                )

    def flush(self, category: str | None = None) -> int:
        """Flush one category or all. Returns total rows written to disk."""
        if not self._enabled:
            return 0
        categories: Iterable[str]
        if category is None:
            categories = list(SCHEMAS.keys())
        elif category not in VALID_CATEGORIES:
            return 0
        else:
            categories = [category]

        total = 0
        for cat in categories:
            total += self._flush_category(cat)
        return total

    def shutdown(self) -> None:
        """Signal the flusher thread to exit and perform a final flush."""
        self._shutdown.set()
        if self._flusher is not None:
            self._flusher.join(timeout=5)
        self.flush()

    # ── Internals ─────────────────────────────────────────────────

    def _flush_category(self, category: str) -> int:
        writer = self._writer_for(category)
        if writer is None:
            return 0

        with self._locks[category]:
            if not self._buffers[category]:
                return 0
            rows = list(self._buffers[category])
            self._buffers[category].clear()

        if not rows:
            return 0

        try:
            df = pd.DataFrame(rows, columns=SCHEMAS[category])
        except Exception as exc:
            logger.warning(
                "[!] LogWriter: could not frame %d rows for %s: %s",
                len(rows), category, exc,
            )
            return 0

        try:
            writer.write_atomic(df, subdirectory=category)
        except Exception as exc:
            logger.warning(
                "[!] LogWriter: write_atomic failed for %s (%d rows): %s",
                category, len(rows), exc,
            )
            # Best-effort: re-enqueue so we don't lose rows on a transient error.
            with self._locks[category]:
                self._buffers[category].extendleft(reversed(rows))
            return 0

        return len(df)


# ─────────────────────────────────────────────────────────────────
# Convenience functions - preferred entry point for most callers.
# ─────────────────────────────────────────────────────────────────


def emit(category: str, row: dict) -> None:
    """Module-level alias for ``LogWriter.get_instance().emit``."""
    LogWriter.get_instance().emit(category, row)


def flush_all() -> int:
    """Flush every category. Useful from shutdown hooks / tests."""
    return LogWriter.get_instance().flush()


def _stringify_config_value(value: Any) -> Any:
    """Always render config old/new values as strings (or None).

    The ``config`` log category accepts heterogeneous value types - bools,
    numbers, dicts (YAML records), lists (search_names), redaction
    sentinels. If those land in a Parquet column as mixed dtypes, pyarrow
    rejects the write with ``Expected bytes, got a 'bool' object``. Casting
    to string up-front keeps the column uniform while preserving enough
    information for ``| search subject=X | table old_value, new_value`` to
    be readable in SPQL.
    """
    if value is None:
        return None
    coerced = _coerce_scalar(value)
    if coerced is None:
        return None
    return coerced if isinstance(coerced, str) else str(coerced)


def log_config_change(
    subject: str,
    action: str,
    *,
    subject_type: str = "setting",
    old_value: Any = None,
    new_value: Any = None,
    actor: str = "system",
    source: str = "",
) -> None:
    """Record a configuration mutation (settings, credentials, YAML stores)."""
    emit("config", {
        "action": action,
        "subject": subject,
        "subject_type": subject_type,
        "old_value": _stringify_config_value(old_value),
        "new_value": _stringify_config_value(new_value),
        "actor": actor,
        "source": source,
    })


def log_search_run(
    search_name: str,
    status: str,
    *,
    row_count: int | None = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
    query_hash: str | None = None,
    triggered_by: str = "scheduler",
) -> None:
    """Record a scheduled saved-search execution."""
    emit("search_runs", {
        "search_name": search_name,
        "status": status,
        "row_count": row_count,
        "duration_ms": duration_ms,
        "error_message": error_message,
        "query_hash": query_hash,
        "triggered_by": triggered_by,
    })


def log_alert_group_event(
    group_name: str,
    status: str,
    *,
    searches_used: list | None = None,
    estimated_tokens: int | None = None,
    actual_tokens: int | None = None,
    cost_usd: float | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    dry_run: bool = False,
    feeder_loop_ms: int | None = None,
    claude_call_ms: int | None = None,
    email_send_ms: int | None = None,
) -> None:
    """Record an alert group dispatch attempt (success, error, skipped).

    Per-phase timings (``feeder_loop_ms``, ``claude_call_ms``,
    ``email_send_ms``) let operators SPQL-aggregate bottleneck analysis
    across many dispatches. Pass ``None`` when a phase didn't run (e.g.
    rate-limited dispatches never reach the feeder loop).
    """
    emit("alert_groups", {
        "group_name": group_name,
        "status": status,
        "searches_used": searches_used,
        "estimated_tokens": estimated_tokens,
        "actual_tokens": actual_tokens,
        "cost_usd": cost_usd,
        "error_message": error_message,
        "duration_ms": duration_ms,
        "dry_run": dry_run,
        "feeder_loop_ms": feeder_loop_ms,
        "claude_call_ms": claude_call_ms,
        "email_send_ms": email_send_ms,
    })


def log_claude_api_call(
    *,
    request_id: str,
    source: str,
    model: str,
    status: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    group_name: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    stop_reason: str | None = None,
    attempt_num: int | None = None,
    retried: bool = False,
    headroom_path: str | None = None,
) -> None:
    """Record the metadata for a single Claude API call.

    Full request + response payloads live in the dedicated SQLite history
    store (``analyzers.claude_history_store``) - this Parquet row is the
    lightweight, SPQL-queryable index for cost alerting and trend analysis.

    ``headroom_path`` records how this attempt was routed -
    ``"headroom"`` (via the compression proxy), ``"direct"`` (straight to
    Anthropic), or ``"direct-fallback"`` (proxy was unreachable and the
    call failed open to direct).
    """
    emit("claude_api", {
        "request_id": request_id,
        "source": source,
        "model": model,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "group_name": group_name,
        "error_class": error_class,
        "error_message": error_message,
        "stop_reason": stop_reason,
        "attempt_num": attempt_num,
        "retried": retried,
        "headroom_path": headroom_path,
    })


def log_ingestion_run(
    task_id: Any,
    title: str,
    status: str,
    *,
    duration_ms: int | None = None,
    error_message: str | None = None,
    row_count: int | None = None,
    attempt: int | None = None,
    trust_level: str | None = None,
) -> None:
    """Record a scheduled input ingestion attempt."""
    emit("ingestion", {
        "task_id": str(task_id),
        "title": title,
        "status": status,
        "duration_ms": duration_ms,
        "error_message": error_message,
        "row_count": row_count,
        "attempt": attempt,
        "trust_level": trust_level,
    })


def log_system_event(
    component: str,
    event: str,
    *,
    level: str = "info",
    message: str = "",
) -> None:
    """Record a system lifecycle event (startup, shutdown, scheduler)."""
    emit("system", {
        "level": level,
        "component": component,
        "event": event,
        "message": message,
    })


def log_patch_suggestion(
    *,
    task_id: Any = "",
    title: str = "",
    error_hash: str = "",
    status: str = "",
    model: str = "",
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    patch: str = "",
    explanation: str = "",
    request_id: str = "",
    error_message: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    drafter_error_class: str = "",
    drafter_error_message: str = "",
) -> None:
    """Phase 4 / Bet 4 slice 8a - record one patch-drafter suggestion.

    Written from ``scheduled_input_engine/engine.py``'s failure-path
    wiring AND from the on-demand ``/api/patch-drafter/suggest``
    endpoint. The schema is intentionally minimal - just enough for
    the operator to review + apply the diff manually + correlate with
    ``claude_api_history.sqlite`` via ``request_id``.

    The ``error_message`` and ``patch`` fields are truncated only by
    Parquet's natural string handling - there's no ceiling here.
    Failed-feeder errors and unified diffs can be long; the operator
    benefits from full context when triaging.
    """
    emit("patch_suggestions", {
        "task_id": str(task_id) if task_id is not None else "",
        "title": title,
        "error_hash": error_hash,
        "status": status,
        "model": model,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "patch": patch,
        "explanation": explanation,
        "request_id": request_id,
        "error_message": error_message,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "drafter_error_class": drafter_error_class,
        "drafter_error_message": drafter_error_message,
    })


def log_ag_pick(
    *,
    alert_group: str,
    run_request_id: str,
    rank_in_brief: int,
    idea_id: str,
    instrument_type: str,
    instrument_id: str,
    direction: str,
    conviction_pct: int,
    expected_return_pct: float,
    position_size_tier: str,
    entry_price: float,
    suggested_buy_epoch: int,
    suggested_sell_epoch: int,
    hold_hours: int,
    take_profit_price: float | None = None,
    stop_loss_price: float | None = None,
    exit_catalyst: str = "",
    thesis: str = "",
    source_signals: str = "",
    pick_tier: str = "TOP",
    correlation_cluster: str = "",
    short_squeeze_risk_json: str = "",
    status: str = "open",
    source: str = "claude",
    model_used: str = "",
    event_timestamp: str | None = None,
    # Options-specific fields (Wave 1 of Options Edge Brief, 2026-04-26).
    # All optional; pass None for non-options picks.
    option_structure: str | None = None,
    option_legs_json: str | None = None,
    option_max_loss_usd: float | None = None,
    option_max_profit_usd: float | None = None,
    option_net_debit_credit: float | None = None,
    option_dte_days: int | None = None,
    option_difficulty_tier: str | None = None,
    account_size_floor_usd: float | None = None,
) -> None:
    """Record a single pick from an alert-group dispatch.

    Written as one row to ``indexes/IMMUTABLE/ag_picks/*.parquet``. The
    dispatcher calls this once per opportunity after extracting the
    fenced JSON block from Claude's response. Columns match the
    ``ag_picks`` schema in ``SCHEMAS``; unknown fields are dropped,
    missing fields land as None.

    The ``event_timestamp`` defaults to the current UTC ISO string if
    not provided. The underlying ``_epoch`` column is auto-filled by
    the log writer.

    All time fields are Unix seconds (int). ``hold_hours`` is
    redundant with ``(suggested_sell_epoch - suggested_buy_epoch) /
    3600`` but is carried as a first-class column so SPQL filters like
    ``| where hold_hours >= 24`` work without an eval.

    Options-specific kwargs (added 2026-04-26 with the Options Edge
    Brief alert group) carry the structured leg / risk / difficulty
    metadata that Wave 2 needs to mark each pick to market. Non-options
    AGs pass them as None and they land as Parquet NULLs.

    Added 2026-04-21 as part of the Daily Opportunity Brief pick-capture
    pipeline. See docs/lang/12_alert_groups.md § Pick Capture.
    """
    import datetime as _dt
    if event_timestamp is None:
        event_timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
    emit("ag_picks", {
        "event_timestamp": event_timestamp,
        "alert_group": alert_group,
        "run_request_id": run_request_id,
        "rank_in_brief": rank_in_brief,
        "pick_tier": pick_tier,
        "idea_id": idea_id,
        "instrument_type": instrument_type,
        "instrument_id": instrument_id,
        "direction": direction,
        "conviction_pct": conviction_pct,
        "expected_return_pct": expected_return_pct,
        "position_size_tier": position_size_tier,
        "entry_price": entry_price,
        "suggested_buy_epoch": suggested_buy_epoch,
        "suggested_sell_epoch": suggested_sell_epoch,
        "hold_hours": hold_hours,
        "take_profit_price": take_profit_price,
        "stop_loss_price": stop_loss_price,
        "correlation_cluster": correlation_cluster,
        "short_squeeze_risk_json": short_squeeze_risk_json,
        "exit_catalyst": exit_catalyst,
        "thesis": thesis,
        "source_signals": source_signals,
        "status": status,
        "source": source,
        "model_used": model_used,
        "option_structure": option_structure,
        "option_legs_json": option_legs_json,
        "option_max_loss_usd": option_max_loss_usd,
        "option_max_profit_usd": option_max_profit_usd,
        "option_net_debit_credit": option_net_debit_credit,
        "option_dte_days": option_dte_days,
        "option_difficulty_tier": option_difficulty_tier,
        "account_size_floor_usd": account_size_floor_usd,
    })


def log_ag_pick_closure(
    *,
    alert_group: str,
    idea_id: str,
    instrument_type: str,
    instrument_id: str,
    outcome: str,
    trigger_rule: str,
    entry_price: float,
    exit_price: float,
    exit_epoch: int,
    pnl_per_contract_usd: float,
    pnl_pct_vs_max_loss: float,
    days_held: float,
    leg_prices_at_close_json: str = "",
    closure_quality: str = "clean",
    account_size_floor_usd: float | None = None,
    fits_account_at_entry: bool | None = None,
    current_account_size_usd_at_close: float | None = None,
    fits_account_at_close: bool | None = None,
    event_timestamp: str | None = None,
) -> None:
    """Record a single pick closure.

    Wave 2 of Options Edge Brief (2026-04-26). Written by the
    ``oeb_pick_tracker_pro`` ingestion script once per pick that hits
    one of the four exit rules:

      * ``stop_loss_hit``  → ``outcome="lost"``
      * ``take_profit_hit`` → ``outcome="won"``
      * ``time_stop``       → ``outcome="time_exit"``
      * ``expiration``      → ``outcome="expired"``

    P&L is journaled per 1 contract (price math, not position math) so
    the metric stays stable as the account scales over the decade
    horizon. Position-size scaling is applied at
    query time, not journal time.

    The dual ``fits_account_*`` columns enable the ``hit_rate_overall``
    vs ``hit_rate_account_fit`` split - picks that didn't fit the
    user's actual account at entry are excluded from the latter metric.
    """
    import datetime as _dt
    if event_timestamp is None:
        event_timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
    emit("ag_picks_closures", {
        "event_timestamp": event_timestamp,
        "alert_group": alert_group,
        "idea_id": idea_id,
        "instrument_type": instrument_type,
        "instrument_id": instrument_id,
        "outcome": outcome,
        "trigger_rule": trigger_rule,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_epoch": exit_epoch,
        "pnl_per_contract_usd": pnl_per_contract_usd,
        "pnl_pct_vs_max_loss": pnl_pct_vs_max_loss,
        "days_held": days_held,
        "leg_prices_at_close_json": leg_prices_at_close_json,
        "closure_quality": closure_quality,
        "account_size_floor_usd": account_size_floor_usd,
        "fits_account_at_entry": fits_account_at_entry,
        "current_account_size_usd_at_close": current_account_size_usd_at_close,
        "fits_account_at_close": fits_account_at_close,
    })


def log_ag_review_observation(
    *,
    alert_group: str,
    run_request_id: str,
    review_period_start: str,
    review_period_end: str,
    review_period_days: int,
    n_picks_overall: int,
    n_picks_account_fit: int,
    hit_rate_overall: float,
    hit_rate_account_fit: float,
    best_signal_class: str = "",
    worst_signal_class: str = "",
    observation_text: str = "",
    observation_evidence: str = "",
    observation_actionable: bool = False,
    rule_tweak_recommendation_text: str = "",
    rule_tweak_rationale: str = "",
    rule_tweak_expected_impact: str = "",
    row_kind: str = "observation",
    event_timestamp: str | None = None,
    calibration_status: str = "",
    calibration_n_closures: int = 0,
) -> None:
    """Record one row from the weekly performance review.

    Wave 2 of Options Edge Brief (2026-04-26). Each weekly dispatch of
    ``options_performance_review`` produces ONE summary row (carrying
    the rule-tweak recommendation + headline metrics) plus N
    observation rows (one per pattern Claude identified). The
    ``row_kind`` column discriminates so SPQL queries can filter to
    just summary rows for trend visualizations.
    """
    import datetime as _dt
    if event_timestamp is None:
        event_timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
    emit("ag_picks_review_observations", {
        "event_timestamp": event_timestamp,
        "alert_group": alert_group,
        "run_request_id": run_request_id,
        "review_period_start": review_period_start,
        "review_period_end": review_period_end,
        "review_period_days": review_period_days,
        "n_picks_overall": n_picks_overall,
        "n_picks_account_fit": n_picks_account_fit,
        "hit_rate_overall": hit_rate_overall,
        "hit_rate_account_fit": hit_rate_account_fit,
        "best_signal_class": best_signal_class,
        "worst_signal_class": worst_signal_class,
        "observation_text": observation_text,
        "observation_evidence": observation_evidence,
        "observation_actionable": observation_actionable,
        "rule_tweak_recommendation_text": rule_tweak_recommendation_text,
        "rule_tweak_rationale": rule_tweak_rationale,
        "rule_tweak_expected_impact": rule_tweak_expected_impact,
        "row_kind": row_kind,
        "calibration_status": calibration_status,
        "calibration_n_closures": calibration_n_closures,
    })


def log_curator_telemetry(
    *,
    event_ts_iso: str,
    event_date: str,
    event_type: str,
    video_external_id: str = "",
    chosen_by: str = "",
    run_date: str = "",
    position: int | None = None,
    slot_kind: str = "",
    watched_seconds: float | None = None,
    total_seconds: float | None = None,
    rating: int | None = None,
    reason: str = "",
    kind: str = "",
    content: str = "",
    query: str = "",
    raw_json: str = "",
) -> None:
    """Record a single speaktube playback / interaction event.

    Written as one row to ``indexes/IMMUTABLE/curator_telemetry/*.parquet``.
    The hourly ``curator_telemetry_pull`` ingestion script parses
    speaktube's NDJSON feed and calls this once per event. Unknown
    fields on the wire are preserved in ``raw_json`` so a schema bump
    on speaktube's side never silently loses data.

    Phase 6 / Bet 5 slice 1 (2026-05-16). See
    ``docs/lang/21_curator_speaktube.md``.
    """
    emit("curator_telemetry", {
        "event_ts_iso": event_ts_iso,
        "event_date": event_date,
        "event_type": event_type,
        "video_external_id": video_external_id,
        "chosen_by": chosen_by,
        "run_date": run_date,
        "position": position,
        "slot_kind": slot_kind,
        "watched_seconds": watched_seconds,
        "total_seconds": total_seconds,
        "rating": rating,
        "reason": reason,
        "kind": kind,
        "content": content,
        "query": query,
        "raw_json": raw_json,
    })


def log_curator_reflection(
    *,
    event_ts_iso: str,
    date: str,
    kind: str,
    content: str,
    video_external_id: str = "",
    source: str = "api_post",
) -> None:
    """Record a user reflection (free-text note).

    Written as one row to ``indexes/IMMUTABLE/curator_reflections/*.parquet``.
    Two entry points feed this: the ``POST /api/reflections`` endpoint
    (``source="api_post"``) and the ``reflection_submit`` telemetry
    event stream (``source="telemetry_event"``).
    """
    emit("curator_reflections", {
        "event_ts_iso": event_ts_iso,
        "date": date,
        "kind": kind,
        "content": content,
        "video_external_id": video_external_id,
        "source": source,
    })


def log_curator_keyword_pref(
    *,
    event_ts_iso: str,
    keyword: str,
    source: str = "api_post",
    raw_request: str = "",
) -> None:
    """Record one user-supplied keyword preference (slice 11, 2026-05-17).

    Written as one row to ``indexes/IMMUTABLE/curator_keyword_prefs/*.parquet``.
    Per the speaktube spec (request #10), the operator POSTs one or
    more keywords; the dispatcher's keyword-boost hook reads the
    "active pool" (rows since the most-recent ``curator_playlist``
    composition OR a 24h fallback) at AG-fire time and boosts
    matching candidates' ``interest_score``.

    Storage is forever (IMMUTABLE) even though the FUNCTIONAL
    relevance "expires after the next composer fire" - historical
    keyword pools tell us how the operator's interests evolved.
    """
    emit("curator_keyword_prefs", {
        "event_ts_iso": event_ts_iso,
        "keyword": keyword,
        "source": source,
        "raw_request": (raw_request or "")[:5000],
    })


def read_active_curator_keyword_pool(
    *,
    fallback_seconds: int = 86400,
) -> list[str]:
    """Slice 11 (2026-05-17 - speaktube req #10): return the active
    keyword pool - keywords POSTed since the most-recent
    ``curator_playlist`` composition, OR (fallback) the trailing
    ``fallback_seconds`` window when no composition exists yet.

    Case-insensitive dedup: ``"Joinery"`` and ``"joinery"`` collapse
    to one entry; the FIRST-posted casing wins. Returns an empty
    list on missing index / DuckDB failure (never raises).

    Imported by both ``desktop_app/server.py::/api/preferences/keywords``
    and ``alert_groups/dispatcher.py::_maybe_apply_keyword_boost`` so
    the endpoint and the dispatcher agree on what counts as
    "currently active".
    """
    try:
        import time as _time
        import duckdb
        from global_settings import get_settings
        settings = get_settings()

        kw_root = settings.immutable_subdir("curator_keyword_prefs")
        if not kw_root.exists():
            return []
        kw_files = sorted(str(p) for p in kw_root.rglob("*.parquet"))
        if not kw_files:
            return []

        # Determine the "since when" cutoff.
        playlist_root = settings.immutable_subdir("curator_playlist")
        cutoff_epoch = None
        # Use a per-call connection. The module-level ``duckdb.sql()``
        # shares a global default connection that is NOT thread-safe;
        # this helper runs in a Flask request handler that may overlap
        # with other endpoints reading via the same global. Caught
        # 2026-05-18 when GET /api/preferences/keywords + GET /api/search
        # firing back-to-back left the global connection in a bad
        # state, producing ``InvalidInputException`` on /api/search.
        # Drift-guarded by tests/test_curator_immutable_read_robustness.py.
        con = duckdb.connect(database=":memory:")
        try:
            con.execute("PRAGMA threads=1")
            if playlist_root.exists():
                pl_files = sorted(str(p) for p in playlist_root.rglob("*.parquet"))
                if pl_files:
                    pl_quoted = ", ".join(f"'{f}'" for f in pl_files)
                    try:
                        # union_by_name=true tolerates schema heterogeneity
                        # across curator IMMUTABLE parquets (empty-fire
                        # parquets infer all-None columns as Null type +
                        # additive schema growth).
                        row = con.execute(
                            f"SELECT MAX(_epoch) "
                            f"FROM read_parquet([{pl_quoted}], union_by_name=true)"
                        ).fetchone()
                        if row and row[0] is not None:
                            cutoff_epoch = int(row[0])
                    except duckdb.Error:
                        cutoff_epoch = None

            if cutoff_epoch is None:
                cutoff_epoch = int(_time.time()) - max(1, int(fallback_seconds))

            kw_quoted = ", ".join(f"'{f}'" for f in kw_files)
            rows = con.execute(
                f"SELECT keyword FROM ("
                f"  SELECT FIRST(keyword ORDER BY _epoch ASC) AS keyword "
                f"  FROM read_parquet([{kw_quoted}], union_by_name=true) "
                f"  WHERE _epoch > {cutoff_epoch} "
                f"  AND keyword IS NOT NULL AND keyword != '' "
                f"  AND source = 'api_post' "
                f"  GROUP BY LOWER(keyword)"
                f") ORDER BY keyword"
            ).fetchall()
        finally:
            con.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def log_curator_playlist_item(
    *,
    run_date: str,
    composed_at_iso: str,
    growth_dial: float,
    theme: str = "",
    position: int,
    slot_kind: str,
    rationale: str,
    external_id: str,
    url: str,
    title: str = "",
    channel_name: str = "",
    thumbnail_url: str = "",
    published_at: str = "",
    duration_seconds: int | None = None,
    interest_score: float | None = None,
    growth_score: float | None = None,
    slop_score: float | None = None,
    score_reasoning: str = "",
    thin_history_active: bool = False,
) -> None:
    """Record a single composed playlist item.

    One call per item; a playlist of N items writes N rows sharing the
    same ``run_date`` + ``composed_at_iso``. The ``GET /api/playlist/today``
    endpoint reads the most-recent ``run_date`` (composer-emitted, not
    write epoch - playlists can be composed late at night for the next
    morning) and reconstructs the JSON shape speaktube expects.

    Slice 4 (2026-05-17): ``thumbnail_url`` and ``published_at`` are
    additive columns threaded from the candidate row through the
    composer's LLM output. Both land empty string when the LLM omits
    them - speaktube falls back gracefully (YouTube synthesis for
    thumbnails, curator-order sort for missing dates).

    Slice 10 (2026-05-17): ``thin_history_active`` records whether
    thin-history aggressive-discovery mode was firing at compose time
    (effective ``growth_dial`` boosted from stored value by
    ``curator_thin_history_dial_bias``). Surfaced in the playlist API
    response so speaktube can show a "thin-history mode" badge.
    Default ``False`` for back-compat with slice-4..9 callers.
    """
    emit("curator_playlist", {
        "run_date": run_date,
        "composed_at_iso": composed_at_iso,
        "growth_dial": growth_dial,
        "theme": theme,
        "position": position,
        "slot_kind": slot_kind,
        "rationale": rationale,
        "external_id": external_id,
        "url": url,
        "title": title,
        "channel_name": channel_name,
        "thumbnail_url": thumbnail_url,
        "published_at": published_at,
        "duration_seconds": duration_seconds,
        "interest_score": interest_score,
        "growth_score": growth_score,
        "slop_score": slop_score,
        "score_reasoning": score_reasoning,
        "thin_history_active": thin_history_active,
    })


def log_curator_topic_cluster(
    *,
    snapshot_epoch: int,
    snapshot_id: str,
    model_name: str,
    dim: int,
    n_clusters: int,
    n_history_rows: int,
    decay_lambda_days: float,
    cluster_id: int,
    centroid_json: str,
    weight: float,
    n_members: int,
    exemplar_titles_json: str,
    label: str = "",
) -> None:
    """Record one cluster of a curator topic snapshot.

    Phase 6 / Bet 5 slice 3 (2026-05-16). The snapshot-refresh job calls
    this once per cluster after :func:`analyzers.topic_vectors.compute_topic_snapshot`
    + (optionally) :func:`label_clusters_with_llm`. Rows from one
    snapshot share ``snapshot_epoch`` + ``snapshot_id``; an SPQL query
    that loads the latest snapshot is one
    ``index="indexes/IMMUTABLE/curator_topic_snapshots/*.parquet" | sort -snapshot_epoch | head N``
    away (N = ``n_clusters`` of the most-recent snapshot).

    ``centroid_json`` carries the embedding vector as a JSON-encoded
    list-of-floats. SPQL doesn't natively query vector columns today,
    so JSON is the lowest-friction format; a future slice can add a
    Phase-1-style sidecar if vector-aware queries become useful.
    """
    emit("curator_topic_snapshots", {
        "snapshot_epoch": snapshot_epoch,
        "snapshot_id": snapshot_id,
        "model_name": model_name,
        "dim": dim,
        "n_clusters": n_clusters,
        "n_history_rows": n_history_rows,
        "decay_lambda_days": decay_lambda_days,
        "cluster_id": cluster_id,
        "centroid_json": centroid_json,
        "weight": weight,
        "n_members": n_members,
        "exemplar_titles_json": exemplar_titles_json,
        "label": label,
    })
