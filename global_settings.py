"""
Global Settings
───────────────
YAML-backed settings store with per-key validation.

User overrides persist in ``global_settings.yaml`` (gitignored).
Defaults are defined in-code and mirrored in ``global_settings.defaults.yaml``
(committed as a reference).
"""

import logging
import os
import re
import threading
from pathlib import Path

import yaml

from functionality.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()

# ── Defaults ──────────────────────────────────────────────────────

DEFAULTS: dict = {
    # Storage
    "indexes_root": "indexes",
    "max_total_size_gb": 200,
    "max_subdirectory_size_gb": 5,
    "max_parquet_file_mb": 128,
    # Logs index (parallel tree under indexes/logs/ with its own budget)
    "logs_root": "indexes/logs",
    "max_logs_size_gb": 5,
    "max_logs_subdirectory_size_gb": 2,
    "logs_enabled": True,
    "logs_flush_interval_seconds": 30,
    # Immutable namespace (Wave 2 of Options Edge Brief, 2026-04-26)
    # Sibling tree under indexes/IMMUTABLE/ excluded from BOTH the indexes
    # and logs cleanup budgets. Used for data the user has explicitly
    # marked as "must survive forever" - pick journal, closure events,
    # performance review observations, future trading-record streams.
    # Available to any ingestion script via settings.immutable_dir() /
    # immutable_subdir(name).
    "immutable_root": "indexes/IMMUTABLE",
    # Trading account sizing (Wave 2 of OEB)
    # Used by the pick tracker + performance review to compute the dual
    # hit-rate (overall vs account-fit). Update this as the account
    # grows from the $1000 starting capital - picks where
    # ``account_size_floor_usd <= current_account_size_usd`` are counted
    # in ``hit_rate_account_fit``; all picks regardless are counted in
    # ``hit_rate_overall``. This number is intended to grow as the
    # account compounds over the decade horizon.
    "current_account_size_usd": 1000.0,
    # Maintenance
    "cleanup_interval_hours": 6,
    # Ingestion
    "default_script_timeout_seconds": 600,
    "max_retries": 3,
    "http_request_timeout_seconds": 30,
    # Per-execution resource budgets
    "max_output_rows": 500_000,
    "max_requests_per_execution": 50,
    "max_response_size_mb": 10,
    # Credentials
    "credential_key_dir": "~/.speakes-query",
    # Subdirectory
    "max_subdirectory_depth": 5,
    # Email / SMTP
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_starttls": True,
    # Security - allowlist of regex patterns matched against the
    # hostname of every outbound HTTP request made from ingestion
    # scripts. Keep in lock-step with the expanded list in
    # ``global_settings.defaults.yaml`` so a fresh install can run the
    # 92 shipped library scripts without manual domain-allowlisting.
    # Adding a new ingestion source → add its domain here AND in the
    # reference yaml.
    "allowed_api_domains": [
        r"^gamma-api\.polymarket\.com$",
        r"^data-api\.polymarket\.com$",
        r"^clob\.polymarket\.com$",
        r"^api\.coingecko\.com$",
        r"^api\.llama\.fi$",
        r"^yields\.llama\.fi$",
        r"^stablecoins\.llama\.fi$",
        r"^api\.stlouisfed\.org$",
        r"^data\.sec\.gov$",
        r"^www\.sec\.gov$",
        r"^efts\.sec\.gov$",
        r"^api\.elections\.kalshi\.com$",
        r"^www\.reddit\.com$",
        r"^wikimedia\.org$",
        r"^api\.nasdaq\.com$",
        r"^api\.massive\.com$",                  # options_unusual_activity_pro (Massive.com / formerly polygon.io)
        r"^api\.polygon\.io$",                   # backward-compat for Massive's prior brand
        # finnhub.io retired 2026-04-25 - Finnhub options chain replaced
        # by Massive after issue #545 documented unresolved 85% mispricing.
        r"^api\.gdeltproject\.org$",
        r"^api\.worldbank\.org$",
        r"^earthquake\.usgs\.gov$",
        r"^api\.weather\.gov$",
        r"^www\.nhc\.noaa\.gov$",
        r"^volcanoes\.usgs\.gov$",
        # Wave 1 alert groups (FXRB / SPBEB / EGIB) - sports betting + energy
        r"^api\.the-odds-api\.com$",
        r"^site\.api\.espn\.com$",
        r"^api\.eia\.gov$",
        # Wave 2 alert groups (PPPB / PHPB) - politics + public health / pharma
        r"^api\.congress\.gov$",
        r"^www\.federalregister\.gov$",
        r"^api\.fda\.gov$",
        r"^clinicaltrials\.gov$",
        # Wave 3 alert groups (SFCB / RCPB / CPB) - science + religion + civilization pulse
        r"^www\.metaculus\.com$",
        r"^export\.arxiv\.org$",
        r"^api\.reporter\.nih\.gov$",
        # Curator / speaktube (Phase 6 / Bet 5 slice 1, 2026-05-16) -
        # the host running the speaktube player + its telemetry-fetch
        # HTTP endpoint. Defaults to same-machine; if speaktube runs on
        # a different LAN host, add that hostname here via
        # global_settings.yaml.
        r"^localhost$",
        # Curator / candidate ingestion (Phase 6 / Bet 5 slice 1.5,
        # 2026-05-16) - YouTube channel RSS feeds. Each subscribed
        # channel exposes its 15 most-recent uploads at
        # https://www.youtube.com/feeds/videos.xml?channel_id=<UC...>.
        # Unauthenticated, free, public.
        r"^www\.youtube\.com$",
        # Curator multi-source - Archive.org (Phase 6 / Bet 5 slice 7,
        # 2026-05-17). Public-domain films, lectures, music, archival
        # video - best-fit for the algorithmic-dignity ethos, no auth,
        # no rate-limit. Used by curator_archive_org_pull for the
        # advancedsearch.php endpoint; yt-dlp resolves the
        # archive.org/details/<id> URLs natively at playback time.
        r"^archive\.org$",
        # Hot-repos alert group (Slice B, 2026-06-23). github.com is
        # scraped for the daily trending list - GitHub has never offered an
        # official trending API. api.github.com (Search API fallback) is
        # already allow-listed above.
        r"^github\.com$",
        # AI paper diffs - Hugging Face (Slice C2, 2026-06-23). The Daily
        # Papers feed (huggingface.co/api/daily_papers) backs /papers/trending.
        r"^huggingface\.co$",
    ],
    # Claude Analyzer
    "claude_analyzer_enabled": False,
    "claude_analyzer_boilerplate_prompt": "",
    "claude_analyzer_model_primary": "claude-sonnet-4-6",
    "claude_analyzer_model_triage": "claude-haiku-4-5-20251001",
    # ── Headroom compression proxy (2026-06-23) ─────────────────────
    # Headroom is a local context-compression proxy that speaks the
    # Anthropic Messages API and forwards to api.anthropic.com, stripping
    # low-information tokens to cut input-token cost. It's a drop-in
    # Anthropic endpoint (same auth, same request/response shape). When
    # enabled, alert-analysis Claude calls are routed through it; on any
    # connection-level failure the call fails open to direct Anthropic.
    # See analyzers/headroom.py + docs/lang/12_alert_groups.md.
    #
    # Global default for whether alert-analysis calls use Headroom.
    # Per-AG ``use_headroom`` overrides this (tri-state). Defaults to False
    # - most installs don't run a Headroom proxy, and routing through a
    # dead proxy adds a failed connection attempt (fail-open to direct)
    # to every call. Turn on only if you self-host Headroom. Env
    # HEADROOM_DISABLE=1 is the operational kill switch.
    "global_use_headroom_default": False,
    # Proxy base URL. Env HEADROOM_PROXY_URL wins over this at runtime; an
    # empty value here falls back to the built-in default. If your proxy
    # host's DNS name resolves to IPv6 but the proxy listens on IPv4 only,
    # use the IPv4 literal.
    "headroom_proxy_url": "http://localhost:8787",
    # Per-call default output cap. Individual alert groups can override via
    # ``max_output_tokens`` on the AG YAML - production Daily Brief-style
    # reports with 5+ opportunities typically need 8192+ to avoid
    # ``stop_reason=max_tokens`` truncation. Raised ceiling 2026-04-20 from
    # 4096 → 32768 after the first production brief got cut off at
    # opportunity #1 with output_tokens=1488.
    "claude_analyzer_max_output_tokens": 8192,
    "claude_analyzer_max_input_rows": 20,
    "claude_analyzer_enable_cache": True,
    "claude_analyzer_enable_batch": False,
    "claude_analyzer_daily_budget_cents": 50,
    "claude_analyzer_spike_threshold": 10.0,
    "claude_analyzer_min_liquidity": 5000.0,
    "claude_analyzer_mv_truncate_limit": 5,
    "claude_analyzer_batch_poll_interval_minutes": 5,
    # Claude API robustness + accounting.
    # Timeout raised 2026-04-21 from 120s after the first live Daily
    # Opportunity Brief run never completed: `tools=web_search` briefs
    # legitimately take 2-5 minutes (Claude chains multiple web_search
    # tool calls inside a single messages.create call). At 120s every
    # attempt hit the wall and the retry loop just burned 8 minutes
    # hitting the same ceiling. 600s gives headroom for a 5-opportunity
    # brief with 10+ web_search invocations; a 10-min retry-exhausted
    # failure only fires when something is truly wrong.
    "claude_request_timeout_seconds": 600,
    "claude_retry_attempts": 3,
    "claude_retry_initial_backoff_seconds": 2,
    "claude_history_retain_payloads": True,
    # Alert group observability / failure alerts
    "alert_group_failure_email_enabled": True,
    "alert_group_failure_email_to": "",
    # Alert group production hardening (2026-04-20 branch)
    "alert_group_max_feeder_staleness_hours": 48,
    "alert_group_fail_on_stale_feeder": False,
    "alert_group_circuit_breaker_consecutive_failures": 5,
    "alert_group_circuit_breaker_auto_disable": True,
    # Maximum number of saved-search feeders allowed in a single alert group.
    # Default 10 matches the original hard-coded cap; operators can raise it
    # up to 100 for tenants that need wider aggregation (e.g. multi-sector
    # briefs). Floor is 2 because a one-feeder "group" is just a scheduled
    # search - the alert-group abstraction is meaningful only at N≥2.
    "alert_group_max_feeders": 10,
    # SEC fair-access fallback (used when credential kind == "contact" missing)
    "sec_edgar_contact_default": "SpeakesQuery EDGAR (noreply@speakesquery.local)",
    # ── Semantic Search (Phase 1 / Bet 2 - slice 5 wires these up) ──
    # Master switch: when False, the embedding sweeper does not run and
    # `| nearest` / `| dedup_semantic` still embed on the fly per call.
    # Default off so existing deployments don't suddenly start populating
    # ~80MB of MiniLM model + N×1.5KB of sidecar parquet without explicit
    # operator opt-in.
    "embeddings_enabled": False,
    # Cap on the embedding-sidecar tree (sum of all *.embeddings.parquet
    # files under indexes/). Independent budget - a runaway sweeper can
    # never evict actual indexed data. Default 5GB is enough for ~3M
    # rows at 384 dims / float32 (~1.5KB per row).
    "max_embeddings_size_gb": 5,
    # HuggingFace model identifier. The default - all-MiniLM-L6-v2 -
    # is small (~80MB), MIT, CPU-friendly, and produces 384-dim vectors.
    # Operators wanting higher recall can swap to BGE-base / Nomic
    # (768 dims, ~270-440MB); see docs/lang/17_semantic_search.md.
    # Note: changing this triggers a full re-embed pass on next sweeper
    # tick - sidecar metadata records the model name for drift detection.
    "embedding_model_name": "sentence-transformers/all-MiniLM-L6-v2",
    # Batch size passed to SentenceTransformer.encode(). Default 32 is
    # CPU-friendly. Operators with a beefy GPU may bump to 128+.
    "embedding_batch_size": 32,
    # How often the embedding sweeper runs (minutes). Floor 1 minute so
    # ingestion lag stays small; ceiling 1440 (one day) so an over-cautious
    # operator can effectively disable without setting embeddings_enabled.
    "embedding_sweep_interval_minutes": 15,
    # ── Notebook reactive cache (Phase 3 / Bet 4 - slice 3) ─────────
    # Master switch for the slice-3 cell-output cache. When False, every
    # `execute_notebook` call re-runs every cell from scratch. When True
    # (default), cells are keyed by content-hash; unchanged inputs serve
    # from the on-disk cache for free. The headline economics promise
    # from ROADMAP Bet 4.2 ("iterating on a brief becomes free until
    # the moment you choose to spend") depends on this being on.
    "notebook_cache_enabled": True,
    # Total disk budget for the notebook cell cache (sum of all pickle
    # payload sizes under <project_root>/notebook_cache/). LRU eviction
    # at the budget boundary. Default 1.0 GB is enough for thousands of
    # typical-sized cells; ceiling 100 GB for power users with very
    # large cached DataFrames.
    "max_notebook_cache_gb": 1.0,
    # ── LLM Pipes (Phase 2 / Bet 3 - slice 7 wires these up) ─────────
    # Hard ceiling on cumulative cost (USD) for any single ``| llm`` /
    # ``| llm_batch`` invocation that doesn't pass an inline
    # ``max_cost_usd=`` kwarg. ``0.0`` means no cap (unlimited). The
    # in-pipe kwarg ALWAYS wins over this default - this is just the
    # implicit fallback for queries that didn't think about cost. The
    # budget gate is conservative-by-design (over-estimates) so the cap
    # is a true hard ceiling, not a soft hint.
    "llm_default_max_cost_usd": 0.0,
    # UI threshold for the "expensive query" warning banner. When a
    # dry-run estimate exceeds this value, the SPA displays a yellow
    # banner asking the operator to confirm. Backend doesn't enforce -
    # only the UI reads this. ``0.0`` disables the warning entirely.
    "llm_warn_above_estimated_usd": 1.0,
    # ── Phase 4 / Bet 4 slice 8a: failed-feeder patch drafter ──────
    # When True, an ingestion task failure triggers a Claude call that
    # asks for a unified-diff fix, recorded to the
    # ``patch_suggestions`` log for the operator to review + apply
    # manually (NEVER auto-applied). Default OFF - opt-in for cost
    # safety. The drafter honors the slice-7 budget gate; cost shows
    # in claude_api_history.sqlite + the patch_suggestions log.
    "patch_drafter_enabled": False,
    "patch_drafter_model": "claude-haiku-4-5-20251001",
    "patch_drafter_max_cost_usd": 0.10,
    "patch_drafter_timeout_seconds": 60,
    # ── Curator / speaktube integration (Phase 6 / Bet 5 slice 1, 2026-05-16) ─
    # The "growth dial" is a BIPOLAR float in [-1.0, +1.0] controlling
    # the playlist composer's exploration vs. familiarity balance
    # (slice 8 bipolar update, 2026-05-17 - was [0.0, 1.0]).
    # -1.0 = stay strictly within the user's known interests (max comfort).
    #  0.0 = balanced - mostly familiar with a few exploratory slots.
    # +1.0 = maximum exploration / never-watched channels (max expansion).
    # The value is set by the speaktube player via
    # ``POST /api/growth_dial`` and read by the playlist-composition
    # alert group at run time. Default -0.7 = "mostly familiar" - the
    # bipolar equivalent of the old 0.15 default's intent. Operators
    # who explicitly set a [0.0, 1.0] value before slice 8 will see it
    # re-interpreted in bipolar terms (positive = exploration-side);
    # re-set via the speaktube slider to restore the old behaviour.
    "curator_growth_dial": -0.7,
    # Base URL of the speaktube sidecar's telemetry endpoint. The
    # hourly ``curator_telemetry_pull`` ingestion script HTTP-fetches
    # NDJSON event lines from ``<base>/api/telemetry/<YYYY-MM-DD>.jsonl``.
    # Empty string disables the pull (default - opt-in until speaktube
    # ships the endpoint).
    "curator_speaktube_base_url": "",
    # How many days of telemetry to back-fill on each ingestion run.
    # 1 = today only; 3 = today + 2 prior days (paranoid catch-up).
    "curator_telemetry_lookback_days": 3,
    # Target playlist size (items) when hybrid expansion fills beyond
    # the LLM's curated picks (slice 6, 2026-05-17). The LLM still
    # composes the top 10-20 items (with rationale + slot_kind); the
    # dispatcher then appends additional items from the scored
    # candidate pool - same external_id dedup, sequential positions -
    # up to this target. Appended items get empty rationale (they're
    # bulk-fill, not LLM-curated) and inherit interest/growth/slop
    # scores from the feeder. Set to 20 to disable expansion entirely
    # (LLM-only output, no appending). Range 20-5000. Composer's
    # max_output_tokens is independent - the LLM never sees this
    # number, so raising it costs nothing extra in LLM tokens.
    "curator_playlist_target_count": 500,
    # Channel diversity cap (slice 9, 2026-05-17 - speaktube req #5).
    # Per-channel ceiling as a fraction of total playlist size; bulk-fill
    # items from over-cap channels are dropped (LLM-curated items count
    # toward the cap but are never dropped - operator can audit via the
    # prompt's 10% rule). For a 500-item playlist, 0.10 caps each
    # channel at 50 items (≈the speaktube spec). Range 0.01-1.0; set
    # to 1.0 to effectively disable the cap.
    "curator_channel_cap_percent": 0.10,
    # Rolling-window same-channel limit (slice 9, 2026-05-17).
    # Within any 10 consecutive positions, no channel may exceed this
    # many items. Default 3 matches the speaktube spec's "no more than
    # 3 items from the same channel within any window of 10
    # positions". Applied only to bulk-fill placement (LLM-curated
    # order is preserved). Range 1-10; set to 10 to effectively
    # disable the window rule.
    "curator_channel_max_in_window": 3,
    # ── Thin-history aggressive discovery (slice 10, 2026-05-17 - speaktube req #12)
    # When the user has watched fewer than `threshold_seconds` of video
    # in the trailing 30 days, the composer's effective growth_dial is
    # boosted by `dial_bias` (clamped to +1.0). This shifts the playlist
    # toward exploration for accounts with thin history - the dial fix
    # alone isn't enough when there's no baseline to bias against.
    # Detection runs at AG-fire time against
    # ``indexes/IMMUTABLE/curator_telemetry/*.parquet``. Speaktube's
    # `thin_history_active` field in the /api/playlist/today response
    # surfaces the state to the renderer.
    "curator_thin_history_enabled": True,
    # 5 hours = 18000s. Below this, thin-history activates. Range
    # 0 - 30 days in seconds. Set to 0 to effectively disable
    # (always thin) or to a very large value to disable (never thin).
    "curator_thin_history_threshold_seconds": 18000,
    # Additive bias on growth_dial when thin-history is active.
    # Default 0.5 shifts -0.7 (mostly familiar) up to -0.2 (slight
    # familiarity bias) - a gentler push than +1.0 max exploration.
    # Range 0.0 - 2.0; the result is clamped to the dial's [-1, +1] range.
    "curator_thin_history_dial_bias": 0.5,
    # ── Keyword preferences (slice 11, 2026-05-17 - speaktube req #10)
    # speaktube's Discover view POSTs keywords here to seed the next
    # composer fire. Storage is IMMUTABLE (forever); the dispatcher's
    # _maybe_apply_keyword_boost reads the "active pool" (rows since
    # the most-recent curator_playlist composition OR a fallback
    # window) and boosts ``interest_score`` on title-matching
    # candidates. "Expires after next composer fire" is functional -
    # the pool resets, the history doesn't.
    "curator_keyword_boost_enabled": True,
    # Additive boost applied to ``interest_score`` for each candidate
    # whose ``title`` contains an active-pool keyword (case-insensitive
    # substring). Applied AFTER topic-scoring so it's an additive
    # overlay. Range 0.0 - 1.0; result is clamped to interest_score's
    # [0, 1] range. 0.0 disables; 0.2 means "matching items rank
    # roughly one tier higher in the LLM's mental model".
    "curator_keyword_boost_amount": 0.2,
    # Fallback lookback when no curator_playlist composition exists
    # yet (fresh install or operator hasn't fired the composer).
    # Default 86400s = 24h. Range 3600s (1h) to 604800s (1 week).
    # Once a composition exists, the dispatcher reads "keywords since
    # the most-recent composed_at_iso" instead.
    "curator_keyword_pool_fallback_seconds": 86400,
    # ── Curator topic vectors (Phase 6 / Bet 5 slice 3, 2026-05-16) ──
    # Master switch for the engine-scheduled topic-snapshot refresh job.
    # Default OFF (opt-in) for the same reason ``embeddings_enabled`` is:
    # the first refresh on a populated history takes ~30-60s of CPU
    # (embedder load + KMeans + per-cluster LLM call), and the snapshot
    # output is only meaningful once a watch_history corpus exists.
    # Operators enable this AFTER importing a Takeout corpus or
    # accumulating live telemetry rows. The CLI tool
    # (``tools/curator_topic_snapshot_refresh``) is always available
    # regardless of this setting.
    "curator_topic_snapshot_refresh_enabled": False,
    # How often the refresh job runs (hours). Default weekly (168h).
    # Topic snapshots are slow-evolving - your "style" doesn't change
    # hour-to-hour. Ceiling 30 days; floor 1 hour for power users.
    "curator_topic_snapshot_refresh_interval_hours": 168,
    # KMeans target cluster count. Empirically 8-12 is the sweet spot
    # for personal histories of 1k-10k watches - fewer collapses
    # distinct topics, more produces near-duplicates. Capped at
    # len(history) at compute time so a sparse bootstrap never crashes.
    "curator_topic_n_clusters": 10,
    # Recency decay half-life in days. With default 180 (≈6-month
    # half-life), a watch from a year ago counts ~0.25× a watch from
    # yesterday in cluster definition. Higher = older watches matter
    # more (slower drift); lower = aggressive recency bias.
    "curator_topic_decay_lambda_days": 180,
    # Registry id for the LLM that labels clusters (cluster_id →
    # 3-5-word human label). Default targets a self-hosted llama.cpp
    # server (Qwen3.5-122B-A10B, endpoint from the model registry).
    # $0 calls when available; falls back to placeholder labels when
    # unreachable.
    "curator_topic_label_model_id": "llamacpp-qwen35-122b-a10b",
    # Per-refresh hard ceiling on labeling cost. The default model is
    # $0 so this is belt-and-suspenders; operators using a cloud
    # labeling model (e.g. claude-haiku-4-5-20251001) want this set.
    "curator_topic_label_max_cost_usd": 0.05,
}

# ── Validators ────────────────────────────────────────────────────
# Each entry: (type, min, max) or (type, None, None) for unbounded.

_INT_VALIDATORS: dict = {
    "max_total_size_gb":              (1, 10_000),
    "max_subdirectory_size_gb":       (1, None),      # capped at max_total dynamically
    "max_parquet_file_mb":            (16, 1024),
    "max_logs_size_gb":               (1, 1000),
    "max_logs_subdirectory_size_gb":  (1, None),      # capped at max_logs_size_gb dynamically
    "logs_flush_interval_seconds":    (5, 600),
    "cleanup_interval_hours":         (1, 168),        # 1 hour floor, 1 week ceiling
    "default_script_timeout_seconds": (10, 600),
    "max_retries":                    (0, 10),
    "http_request_timeout_seconds":   (5, 300),
    "max_subdirectory_depth":         (5, 20),
    "max_output_rows":                (1_000, 10_000_000),
    "max_requests_per_execution":     (1, 500),
    "max_response_size_mb":           (1, 100),
    "smtp_port":                      (1, 65535),
    # Claude Analyzer
    "claude_analyzer_max_output_tokens":  (128, 32768),
    "claude_analyzer_max_input_rows":     (1, 1000),
    "claude_analyzer_daily_budget_cents": (1, 10000),
    "claude_analyzer_mv_truncate_limit":  (1, 50),
    "claude_analyzer_batch_poll_interval_minutes": (1, 60),
    # Claude robustness
    "claude_request_timeout_seconds":        (10, 3600),
    "claude_retry_attempts":                 (0, 10),
    "claude_retry_initial_backoff_seconds":  (1, 60),
    # Alert group hardening
    "alert_group_max_feeder_staleness_hours":        (1, 720),
    "alert_group_circuit_breaker_consecutive_failures": (1, 100),
    "alert_group_max_feeders":                       (2, 100),
    # Semantic search budgets + cadence (slice 5)
    "max_embeddings_size_gb":          (1, 1000),
    "embedding_batch_size":            (1, 1024),
    "embedding_sweep_interval_minutes": (1, 1440),
    # Curator / speaktube
    "curator_telemetry_lookback_days": (1, 30),
    # Curator topic vectors (slice 3)
    "curator_topic_snapshot_refresh_interval_hours": (1, 24 * 30),
    "curator_topic_n_clusters":                       (2, 100),
    "curator_topic_decay_lambda_days":                (1, 3650),
    # Curator playlist hybrid expansion (slice 6, 2026-05-17)
    "curator_playlist_target_count":                  (20, 5000),
    # Curator channel cooldown (slice 9, 2026-05-17 - speaktube req #5)
    "curator_channel_max_in_window":                  (1, 10),
    # Curator thin-history threshold (slice 10, 2026-05-17 - speaktube req #12)
    # 0 to 30 days expressed in seconds (30 * 86400 = 2,592,000)
    "curator_thin_history_threshold_seconds":         (0, 2_592_000),
    # Curator keyword-prefs fallback lookback (slice 11, 2026-05-17 - speaktube req #10)
    # 1 hour to 1 week expressed in seconds
    "curator_keyword_pool_fallback_seconds":          (3_600, 604_800),
}


# Float settings that need range validation but aren't in
# _INT_VALIDATORS (which is int-typed). Generic validator branch
# (`_validate_key`) reads this for the few float settings that share
# the (lo, hi) shape - keeps the per-key custom branches focused on
# settings with cross-field rules.
_FLOAT_VALIDATORS: dict = {
    # Slice 9 - see DEFAULTS comment above
    "curator_channel_cap_percent": (0.01, 1.0),
    # Slice 10 - see DEFAULTS comment above
    "curator_thin_history_dial_bias": (0.0, 2.0),
    # Slice 11 - see DEFAULTS comment above
    "curator_keyword_boost_amount": (0.0, 1.0),
}


# Keys whose values are secrets - redact from the logs index so the Parquet
# stream doesn't end up replaying plaintext passwords during a cost-query.
_SECRET_KEYS = frozenset({"smtp_password"})


def _redact_for_log(key: str, value):
    """Return a value safe to record in the config log."""
    if key in _SECRET_KEYS:
        if value:
            return f"<redacted:{len(str(value))} chars>"
        return "<empty>"
    return value


def _emit_config_change_safely(key: str, action: str, old_value, new_value):
    """Write a config-change log row, never raising back to the caller.

    The settings layer is a hot path and must not fail just because the log
    writer is unavailable during early startup or under unusual I/O
    conditions.
    """
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=key,
            action=action,
            subject_type="setting",
            old_value=_redact_for_log(key, old_value),
            new_value=_redact_for_log(key, new_value),
            actor="api",
            source="global_settings",
        )
    except Exception as exc:
        # Config mutations must not be blocked by log-writer failures,
        # but don't swallow silently either - surface the reason at
        # WARNING so the operator can spot a misbehaving log subsystem.
        # (Prior behaviour: bare ``pass`` hid even NotADirectoryError or
        # permission errors in the logs tree.)
        logger.warning(
            "[!] Config-change audit log failed for key %r action=%r: %s",
            key, action, exc,
        )


def _normalise_value(key: str, value):
    """Normalise incoming setting values before validation + persistence.

    Gmail App Passwords are displayed in the Google UI as four groups of
    four lowercase alphanumerics separated by spaces ("frzm amtz omqi sazp").
    A copy-paste brings the spaces along, and Gmail's SMTP server rejects
    AUTH with the spaced form on some regional endpoints. Strip all
    whitespace from the password on the way in so the saved value is the
    canonical 16-char form.

    ``smtp_user`` and ``smtp_from`` are similarly stripped of surrounding
    whitespace - no valid email address contains leading/trailing spaces.
    """
    if key == "smtp_password" and isinstance(value, str):
        return "".join(value.split())
    if key in ("smtp_user", "smtp_from", "smtp_server") and isinstance(value, str):
        return value.strip()
    return value


def _validate_key(key: str, value, all_settings: dict) -> str | None:
    """Return an error message if *value* is invalid for *key*, else ``None``."""
    if key in _INT_VALIDATORS:
        if not isinstance(value, int):
            return f"{key}: must be an integer, got {type(value).__name__}"
        lo, hi = _INT_VALIDATORS[key]
        if lo is not None and value < lo:
            return f"{key}: minimum is {lo}, got {value}"
        if hi is not None and value > hi:
            return f"{key}: maximum is {hi}, got {value}"
    # Float-range validators (slice 9, 2026-05-17 - analog of _INT_VALIDATORS
    # for settings whose contract is a simple float range without
    # cross-field rules). Bool guard explicit because isinstance(True, int)
    # is True in Python; accepting True/False as 1.0/0.0 silently has
    # surprised callers before.
    if key in _FLOAT_VALIDATORS:
        if isinstance(value, bool):
            return f"{key}: must be a number, got bool"
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number, got {type(value).__name__}"
        lo, hi = _FLOAT_VALIDATORS[key]
        if lo is not None and value < lo:
            return f"{key}: minimum is {lo}, got {value}"
        if hi is not None and value > hi:
            return f"{key}: maximum is {hi}, got {value}"
        # Cross-field: subdir <= total
        if key == "max_subdirectory_size_gb":
            total = all_settings.get("max_total_size_gb", DEFAULTS["max_total_size_gb"])
            if value > total:
                return f"{key}: cannot exceed max_total_size_gb ({total})"
        if key == "max_logs_subdirectory_size_gb":
            total = all_settings.get(
                "max_logs_size_gb", DEFAULTS["max_logs_size_gb"]
            )
            if value > total:
                return f"{key}: cannot exceed max_logs_size_gb ({total})"

    elif key in ("indexes_root", "logs_root", "immutable_root"):
        if not isinstance(value, str) or not value.strip():
            return f"{key}: must be a non-empty string"

    elif key == "current_account_size_usd":
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number, got {type(value).__name__}"
        if value <= 0:
            return f"{key}: must be > 0, got {value}"

    elif key == "credential_key_dir":
        if not isinstance(value, str) or not value.strip():
            return f"{key}: must be a non-empty string"

    elif key in ("smtp_server", "smtp_user", "smtp_password", "smtp_from"):
        if not isinstance(value, str):
            return f"{key}: must be a string"

    elif key in ("smtp_starttls", "logs_enabled",
                  "claude_history_retain_payloads",
                  "alert_group_failure_email_enabled",
                  "alert_group_fail_on_stale_feeder",
                  "alert_group_circuit_breaker_auto_disable",
                  "embeddings_enabled",
                  "notebook_cache_enabled",
                  "curator_thin_history_enabled",   # slice 10 2026-05-17
                  "curator_keyword_boost_enabled",   # slice 11 2026-05-17
                  "global_use_headroom_default"):    # headroom 2026-06-23
        if not isinstance(value, bool):
            return f"{key}: must be true or false"

    elif key in ("alert_group_failure_email_to",
                  "sec_edgar_contact_default",
                  "embedding_model_name"):
        if not isinstance(value, str):
            return f"{key}: must be a string"
        if key == "embedding_model_name" and not value.strip():
            return f"{key}: must be a non-empty string"

    elif key == "allowed_api_domains":
        if not isinstance(value, list):
            return f"{key}: must be a list of regex pattern strings"
        for i, pattern in enumerate(value):
            if not isinstance(pattern, str):
                return f"{key}[{i}]: must be a string"
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"{key}[{i}]: invalid regex: {exc}"

    elif key in ("claude_analyzer_enabled", "claude_analyzer_enable_cache",
                  "claude_analyzer_enable_batch"):
        if not isinstance(value, bool):
            return f"{key}: must be true or false"

    elif key in ("claude_analyzer_boilerplate_prompt", "claude_analyzer_model_primary",
                  "claude_analyzer_model_triage"):
        if not isinstance(value, str):
            return f"{key}: must be a string"

    elif key in ("claude_analyzer_spike_threshold", "claude_analyzer_min_liquidity"):
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number"
        if value < 0:
            return f"{key}: must be non-negative"

    elif key in ("llm_default_max_cost_usd", "llm_warn_above_estimated_usd"):
        # Slice 7 - non-negative float; 0.0 means "no cap" / "no warn".
        # Ceiling 1000.0 USD is comfortably above any sane single-pipe
        # cost; keeps a typo from disabling the gate effectively.
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number"
        if value < 0:
            return f"{key}: must be non-negative"
        if value > 1000.0:
            return f"{key}: maximum is 1000.0 USD, got {value}"

    elif key == "patch_drafter_enabled":
        if not isinstance(value, bool):
            return f"{key}: must be true or false"

    elif key == "patch_drafter_model":
        if not isinstance(value, str) or not value.strip():
            return f"{key}: must be a non-empty string"

    elif key == "patch_drafter_max_cost_usd":
        # Same shape as llm_default_max_cost_usd: 0 = uncapped, but
        # NOT recommended for the patch drafter (defaults to 0.10).
        if isinstance(value, bool):
            return f"{key}: must be a number"
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number"
        if value < 0:
            return f"{key}: must be non-negative"
        if value > 1000.0:
            return f"{key}: maximum is 1000.0 USD, got {value}"

    elif key == "patch_drafter_timeout_seconds":
        if isinstance(value, bool):
            return f"{key}: must be an integer"
        if not isinstance(value, int):
            return f"{key}: must be an integer"
        if value < 5:
            return f"{key}: minimum is 5 seconds, got {value}"
        if value > 600:
            return f"{key}: maximum is 600 seconds, got {value}"

    elif key == "curator_growth_dial":
        # Phase 6 / Bet 5 slice 1 (original 0..1) + slice 8 (bipolar
        # -1..+1, 2026-05-17) - exploration knob. -1.0 = max
        # familiarity ("only channels the user watches a lot"); 0.0 =
        # balanced; +1.0 = max exploration ("only never-watched
        # channels"). Written by speaktube via POST /api/growth_dial;
        # read by playlist composer. Bool rejected explicitly so a
        # "yes/no" doesn't silently turn into 0.0/1.0 and surprise
        # the composer.
        if isinstance(value, bool):
            return f"{key}: must be a number"
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number"
        if value < -1.0 or value > 1.0:
            return f"{key}: must be in [-1.0, +1.0], got {value}"

    elif key == "curator_speaktube_base_url":
        if not isinstance(value, str):
            return f"{key}: must be a string"
        if value.strip() and not (value.startswith("http://") or value.startswith("https://")):
            return f"{key}: must start with http:// or https:// (or be empty to disable pull)"

    elif key == "headroom_proxy_url":
        # Headroom compression proxy base URL. Empty falls back to the
        # built-in default in analyzers/headroom.py; a non-empty value
        # must be a full http(s) URL so a typo surfaces at save time.
        if not isinstance(value, str):
            return f"{key}: must be a string"
        if value.strip() and not (value.startswith("http://") or value.startswith("https://")):
            return f"{key}: must start with http:// or https:// (or be empty to use the default)"

    elif key == "max_notebook_cache_gb":
        # Phase 3 slice 3 - notebook cell-output cache budget in GB.
        # Floor 0.1 (must allow at least 100 MB of cache to be useful);
        # ceiling 100 GB (above any reasonable use; prevents typo
        # disabling LRU eviction). Bool rejected explicitly.
        if isinstance(value, bool):
            return f"{key}: must be a number"
        if not isinstance(value, (int, float)):
            return f"{key}: must be a number"
        if value < 0.1:
            return f"{key}: minimum is 0.1 GB, got {value}"
        if value > 100.0:
            return f"{key}: maximum is 100.0 GB, got {value}"

    elif key not in DEFAULTS:
        return f"Unknown setting: {key}"

    return None


# ── Singleton ─────────────────────────────────────────────────────

_instance: "GlobalSettings | None" = None
_lock = threading.Lock()


def get_settings(project_root: str | Path | None = None) -> "GlobalSettings":
    """Return (or create) the global settings singleton."""
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is None:
            root = Path(project_root) if project_root else _PROJECT_ROOT
            _instance = GlobalSettings(root)
    return _instance


class GlobalSettings:
    """Thread-safe YAML-backed settings with validation."""

    def __init__(self, project_root: Path):
        self._project_root = project_root.resolve()
        self._path = self._project_root / "global_settings.yaml"
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._load()

    # ── I/O ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load overrides from disk (if file exists), merge with defaults."""
        overrides: dict = {}
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh)
                    if isinstance(raw, dict):
                        overrides = raw
            except Exception as exc:
                logger.error("[x] Failed to read %s: %s", self._path, exc)
        self._cache = {**DEFAULTS, **overrides}

    def _flush(self) -> None:
        """Write current settings to YAML, excluding keys that match defaults."""
        overrides = {}
        for key, value in self._cache.items():
            if value != DEFAULTS.get(key):
                overrides[key] = value
        try:
            text = yaml.dump(overrides, default_flow_style=False, sort_keys=False)
            write_text_atomic(self._path, text)
            logger.info("[i] Settings saved to %s", self._path)
        except Exception as exc:
            logger.error("[x] Failed to write %s: %s", self._path, exc)
            raise

    # ── Public API ────────────────────────────────────────────────

    def get(self, key: str):
        """Return the value for *key* (falls back to default)."""
        with self._lock:
            return self._cache.get(key, DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        """Validate and persist a single setting."""
        with self._lock:
            value = _normalise_value(key, value)
            merged = {**self._cache, key: value}
            err = _validate_key(key, value, merged)
            if err:
                raise ValueError(err)
            old_value = self._cache.get(key)
            self._cache[key] = value
            self._flush()
        _emit_config_change_safely(key, "set", old_value, value)

    def update(self, updates: dict) -> dict:
        """Validate and persist multiple settings at once.

        Returns a dict of ``{key: error_message}`` for any that failed.
        Valid keys are still applied.
        """
        old_values: dict = {}
        new_values: dict = {}
        with self._lock:
            errors: dict = {}
            merged = {**self._cache}
            for key, value in updates.items():
                merged[key] = _normalise_value(key, value)
            for key, value in list(merged.items()):
                if key not in updates:
                    continue
                err = _validate_key(key, value, merged)
                if err:
                    errors[key] = err
                else:
                    old_values[key] = self._cache.get(key)
                    new_values[key] = value
                    self._cache[key] = value
            if not errors or len(errors) < len(updates):
                self._flush()
        for key, new_val in new_values.items():
            _emit_config_change_safely(
                key, "set", old_values.get(key), new_val,
            )
        return errors

    def get_all(self) -> dict:
        """Return all settings (defaults + overrides)."""
        with self._lock:
            return dict(self._cache)

    def reset(self, key: str) -> None:
        """Reset a single key to its default value."""
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        with self._lock:
            old_value = self._cache.get(key)
            self._cache[key] = DEFAULTS[key]
            self._flush()
        _emit_config_change_safely(key, "reset", old_value, DEFAULTS[key])

    def reset_all(self) -> None:
        """Reset all settings to defaults."""
        with self._lock:
            self._cache = dict(DEFAULTS)
            self._flush()
        _emit_config_change_safely("(all)", "reset_all", None, None)

    # ── Derived helpers ───────────────────────────────────────────

    def indexes_dir(self) -> Path:
        """Return the resolved indexes directory path."""
        rel = self.get("indexes_root")
        return (self._project_root / rel).resolve()

    def logs_dir(self) -> Path:
        """Return the resolved logs directory path.

        Guaranteed distinct from ``indexes_dir()`` (or more precisely: not a
        parent or equal of it) so the logs budget can be enforced without
        colliding with the main indexes budget. Callers should ``mkdir
        parents=True, exist_ok=True`` before writing.
        """
        rel = self.get("logs_root")
        return (self._project_root / rel).resolve()

    def immutable_dir(self) -> Path:
        """Return the resolved immutable directory path.

        Wave 2 of Options Edge Brief (2026-04-26): this is the namespace for
        data the user has explicitly marked as 'must survive forever' - the
        pick journal, closure events, performance review observations, and
        any future trading-record streams. Excluded from BOTH the main
        indexes cleanup AND the logs cleanup so it never gets garbage
        collected by mtime-based eviction. Available to any ingestion
        script via this method or :meth:`immutable_subdir`.
        """
        rel = self.get("immutable_root")
        return (self._project_root / rel).resolve()

    def immutable_subdir(self, name: str) -> Path:
        """Return ``immutable_dir() / name``.

        Convenience for ingestion scripts that want to write to a named
        sub-stream under the immutable namespace, e.g. ``ag_picks``,
        ``ag_picks_closures``, ``trade_journal``. The caller is responsible
        for ``mkdir(parents=True, exist_ok=True)`` before writing.
        """
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(
                f"immutable_subdir name must be a simple non-empty identifier, "
                f"got {name!r}"
            )
        return self.immutable_dir() / name

    def credential_key_dir(self) -> Path:
        """Return the resolved credential key directory."""
        return Path(self.get("credential_key_dir")).expanduser().resolve()
