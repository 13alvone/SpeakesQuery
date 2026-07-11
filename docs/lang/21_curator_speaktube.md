# Curator ↔ speaktube contract

**Phase 6 / Bet 5 slice 1 (2026-05-16).** SpeakesQuery acts as the curator host for the **speaktube** LAN-local YouTube-replacement player. This page is the operator's reference for the four REST endpoints, the IMMUTABLE storage layout, the Google Takeout bootstrap import, and the hourly telemetry pull.

The contract is **pull-based**: speaktube polls SpeakesQuery; SpeakesQuery polls speaktube's telemetry log over HTTP. There is no webhook, no push, no shared filesystem. Both sides treat each fetch as authoritative-for-now.

## Hosts

| Component | Address | Role |
|---|---|---|
| **speaktube** | `http://localhost:8080/` (or your LAN host) | Player + telemetry-writing sidecar |
| **SpeakesQuery** | `http://localhost:5111/` | Curator: scoring + composition + dignity score |

The speaktube sidecar reverse-proxies `/api/*` → SpeakesQuery, so the player code never talks to SpeakesQuery directly.

## REST endpoints

All four routes live in [`desktop_app/server.py`](../../desktop_app/server.py).

### `GET /api/playlist/today`

The day's curated playlist. 404 when no composition has happened yet - speaktube renders that as "Curator endpoint not available yet" rather than a misleading "0 items today".

Optional `?date=YYYY-MM-DD` returns the playlist for a specific date (used by tests + operator inspection).

Response:

```json
{
  "run_date": "2026-05-16",
  "growth_dial": 0.18,
  "theme": "1997_cable_surf",
  "items": [
    {
      "position": 1,
      "slot_kind": "main",
      "rationale": "Strong alignment with your stated interest in joinery.",
      "video": {
        "external_id": "dQw4w9WgXcQ",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "title": "Japanese joinery: making a hand plane from scratch",
        "channel_name": "Workshop Companion",
        "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        "published_at": "2026-05-15T16:30:00+00:00",
        "duration_seconds": 1742,
        "interest_score": 0.91,
        "growth_score": 0.05,
        "slop_score": 0.02,
        "score_reasoning": "High channel affinity; slow-pacing markers."
      }
    }
  ]
}
```

Reads `indexes/IMMUTABLE/curator_playlist/*.parquet`, finds the most-recent `run_date`, sorts items by `position` ASC.

`thumbnail_url` and `published_at` were added in slice 4 (2026-05-17). Both are non-elided keys but may carry empty string when the candidate row's source didn't supply them. The speaktube player falls back gracefully - YouTube synthesis for missing thumbnails, curator-order sort when `published_at` is empty. New fields must always be non-elided keys; renaming or removing a field is a breaking contract change.

**Dispatcher hygiene guarantees (slice 5, 2026-05-17):**
* **`external_id` is unique** within a composition. The composer occasionally emits the same `external_id` twice with different rationales; the dispatcher's parser runs keep-first dedup, drops the duplicate with a warning log, and the IMMUTABLE parquet only carries one row per video. Speaktube's defensive client-side dedup is now redundant on the SpeakesQuery side but harmless.
* **`position` is 1-indexed sequential** within the items array. The LLM may emit non-unique (all `1`s) or non-sequential (`5, 10, 15`) values; the parser overwrites with the canonical sequence after dedup so speaktube can trust the field instead of falling back to `idx + 1`.

**Hybrid expansion (slice 6, 2026-05-17):**

The LLM composes the top 10-20 items (with rationale + `slot_kind` + `score_reasoning`). The dispatcher then **appends** additional rows from the scored-candidate pool to reach `curator_playlist_target_count` (default 500). Speaktube gets a long-tail playlist without the LLM having to author 500 rationales - keeping the per-fire cost ~$0.02 (Sonnet 4.6).

* **LLM rows** (positions 1..~20): full rationale + slot_kind from the LLM's compose pass.
* **Bulk rows** (positions ~21..target_count): empty `rationale`, `slot_kind="main"`, `score_reasoning=""`, scores inherited from the feeder's `interest_score` / `growth_score` / `slop_score` columns. `thumbnail_url` + `published_at` are copied from the feeder's candidate row (same column-rename rule: feeder's `published_iso` → playlist's `published_at`).
* **Dedup across both sections**: external_ids picked by the LLM are excluded from the bulk-fill pool, so each video appears at most once.
* **Shared `composed_at_iso`** between the two sections - `/api/playlist/today`'s "find latest composition" filter groups them as one composition.
* **Small-pool semantics**: if the scored pool has fewer rows than (target - LLM count), the dispatcher writes what it has and stops. No synthetic padding.
* **Setting `curator_playlist_target_count = 20`** disables bulk-fill effectively (LLM-only output). Range 20-5000.

The speaktube renderer reads `rationale` truthiness to differentiate "LLM curated" cards (show rationale, "Why this is here" disclosure) from bulk-fill cards (compact display). Both share the same JSON shape - `rationale: ""` is the signal.

**Thin-history aggressive discovery (slice 10, 2026-05-17 - speaktube req #12):**

Two bugs the audit ask (req #12.3) called out get fixed in this slice, plus the new thin-history feature.

* **Dial injection** (audit fix): pre-slice-10, the composer prompt had hard-coded `"defaults to -0.7"` text and the operator's slider had ZERO effect on composition. Speaktube's VM round 4 reported `growth_dial: +0.40` with familiar channels still dominating - that's why. Slice 10 makes the dispatcher substitute `$GROWTH_DIAL_VALUE` and `$THIN_HISTORY_ACTIVE` placeholders in the prompt at AG-fire time, so the LLM sees the current slider value verbatim and composes for it.
* **Thin-history detection**: at dispatch time, the dispatcher sums `watched_seconds` from `indexes/IMMUTABLE/curator_telemetry/` for the trailing 30 days. If the total is below `curator_thin_history_threshold_seconds` (default 18000 = 5 hours), thin-history is active.
* **Dial boost**: when thin-history fires, the dispatcher injects an EFFECTIVE dial value = `clamp(stored_dial + curator_thin_history_dial_bias, -1.0, +1.0)`. The default bias of 0.5 shifts -0.7 ("mostly familiar") to -0.2 ("slight familiarity, room for exploration"). The composer prompt has an explicit "THIN-HISTORY MODE" section that tells the LLM to lean further into unfamiliar channels even at the boosted dial value - the boost is a hint, not a ceiling.

The IMMUTABLE journal records the **effective** dial (what the LLM composed for) AND a `thin_history_active` bool column. `/api/playlist/today` surfaces three related fields:
* `growth_dial` - the EFFECTIVE value at compose time (what was logged in parquet; what the LLM was instructed to use)
* `growth_dial_stored` - the operator's current slider value (read from settings at request time)
* `thin_history_active` - bool, was thin-history boosting active at compose time

Speaktube can render the divergence between effective and stored - e.g. "Your slider is at -0.7 but we boosted to -0.2 because you've only watched 2 hours in the last 30 days." The dial isn't lying; it's adapting to telemetry.

**Operator controls (3 settings):**
* `curator_thin_history_enabled` (bool, default True) - master switch
* `curator_thin_history_threshold_seconds` (int, default 18000 = 5h, range 0 - 2,592,000 = 30 days) - sum below this fires thin-history
* `curator_thin_history_dial_bias` (float, default 0.5, range 0.0 - 2.0) - additive boost; result clamped to [-1, +1]

Set `enabled=false` to use the dial verbatim regardless of telemetry. Set `threshold=0` to never fire. Set `threshold=2592000` to ALWAYS fire (effectively always boost).

**Channel cooldown (slice 9, 2026-05-17 - speaktube req #5):**

Speaktube's VM round 3: *"too many repeat authors/sources on the main page. It should be more randomized."* The dispatcher's bulk-fill path enforces two diversity rules so the long-tail playlist doesn't cluster around 3-4 channels:

1. **Per-channel cap** - each channel's total appearances (LLM + bulk) must stay ≤ `curator_channel_cap_percent * target_count`. Default 0.10 caps each channel at 50 items for a 500-item playlist (matches speaktube's spec). LLM-curated picks count toward the cap but are never dropped; bulk candidates from over-cap channels skip. Set to `1.0` to disable.
2. **Rolling-window dispersal** - within any 10 consecutive positions, no channel exceeds `curator_channel_max_in_window` items (default 3). Bulk placement is greedy: the dispatcher walks the scored candidate pool and skips items whose channel already occupies 3 of the last 9 positions, picking the highest-priority valid candidate instead. The window seeds with the last 9 LLM channels so the LLM→bulk transition respects the rule.

**Graceful degradation:** when the candidate pool's diversity runs out toward the tail (e.g. only one channel's items remain), the algorithm places them anyway with a logged warning rather than truncating the playlist. The user prefers a longer playlist with a slight rule violation at the end over a strictly-compliant shorter playlist.

**LLM-side cap:** the composer prompt also tightens the per-channel cap from the slice-3 30% rule to 10% so the LLM aims for the same target the dispatcher enforces on bulk. The dispatcher doesn't reorder LLM items - their curator-intended sequence is load-bearing - so the prompt rule is the only knob to prevent LLM-side clustering.

### `GET /api/dignity/today`

Today's algorithmic-dignity score - share of plays whose `chosen_by` indicates intentional pick (`curator` / `user_manual` / `playlist`) vs passive surfacing (`recommendation` / empty).

Always returns 200. When no plays have been recorded yet, `dignity_pct` is `null` and counts are zero - speaktube renders both as "offline".

Optional `?date=YYYY-MM-DD` overrides the default of server-local today.

Response:

```json
{ "dignity_pct": 78.2, "total_plays": 14, "chosen_plays": 11 }
```

Reads `indexes/IMMUTABLE/curator_telemetry/*.parquet`, filters to `event_date`, counts `event_type IN ('play_start', 'play_end')`.

### `POST /api/reflections`

Free-text user reflection. Body:

```json
{ "date": "2026-05-16", "kind": "eod", "content": "Today I noticed..." }
```

`kind` is `"eod"` or `"per_video"`. The `per_video` kind requires `video_external_id`. Returns 201 with `{status, id, date, kind}`.

Writes to `indexes/IMMUTABLE/curator_reflections/*.parquet` with `source="api_post"`. The same store also receives `source="telemetry_event"` rows when speaktube emits a `reflection_submit` telemetry event - both pathways are queryable uniformly.

### `GET /api/search?q=...` (slice 12, 2026-05-17 - speaktube req #11)

Ad-hoc cross-source search against the already-ingested candidate pool. Returns the same JSON shape as `/api/playlist/today` so the speaktube renderer reuses one code path.

**Query parameters:**
* `q` (required, urlencoded) - whitespace-separated tokens. At least ONE token must match the candidate's `title` (case-insensitive substring; tokens OR'd). Empty or missing `q` → 400.
* `sources` (optional, comma-separated) - restrict to specific `source` enum values (e.g. `?sources=youtube_rss,archive_org`). Default: all sources.
* `limit` (optional) - soft cap on items. Default 100; max 1000.

**Response (200 even when empty):**

```json
{
  "run_date": "2026-05-17",
  "growth_dial": -0.7,
  "growth_dial_stored": -0.7,
  "thin_history_active": false,
  "theme": "",
  "items": [
    {
      "position": 1,
      "slot_kind": "main",
      "rationale": "",
      "video": {
        "external_id": "...",
        "url": "...",
        "title": "...",
        "channel_name": "...",
        "thumbnail_url": "...",
        "published_at": "...",
        "duration_seconds": null,
        "interest_score": 1.0,
        "growth_score": null,
        "slop_score": 0.1,
        "score_reasoning": "Matched search: <q>"
      }
    }
  ]
}
```

**Semantics:**

* Searches `indexes/IMMUTABLE/curator_candidates/*.parquet` - no real-time yt-dlp shell-outs (per the speaktube spec: too slow + rate-limit-prone for ad-hoc UX). Cross-source results require multi-source ingestion to have run (slice 7 onward - Archive.org Tier 1 is the first non-YouTube source).
* Results sorted by `_epoch DESC` (most-recently-discovered first). This is publish-date for YouTube RSS rows, add-to-archive date for Archive.org rows. Within-source recency, NOT cross-source ranking.
* Info-rows from ingestion scripts (`source` ending in `_info`) are excluded from results - the operator doesn't want to see "no subscriptions found" pseudo-videos when they search.
* `interest_score=1.0` for all rows (the user explicitly asked); `growth_score=null` (not meaningful here); `slop_score` computed via the same regex as the composer feeder (so the slop badge renders consistently).
* `score_reasoning` carries the matched query for inline display.
* User input with regex special characters (`C++`, `(x)`, `1+1`) is treated as a literal substring - each token is `re.escape()`'d before joining into the alternation pattern.

**No new endpoint configuration** - speaktube can query immediately. The endpoint reads existing candidate ingestion + applies the same heuristics the composer would.

### `POST /api/preferences/keywords` (slice 11, 2026-05-17 - speaktube req #10)

Operator-supplied keywords that bias the next composer fire. Body:

```json
{ "keywords": ["rare earth magnets", "public-domain noir"] }
```

Each keyword writes one row to `indexes/IMMUTABLE/curator_keyword_prefs/*.parquet` with `source="api_post"`. Storage is **forever**; the FUNCTIONAL relevance "expires" after the next composer fire (those keywords no longer count as "active" but the historical row stays for analytics).

**Case-insensitive dedup:** `"Joinery"` and `"joinery"` collapse to one entry; the FIRST-posted casing wins. Dedup happens both within a single request AND against the existing active pool.

**Response:**

```json
{
  "status": "success",
  "added": 1,         // how many writes happened
  "skipped": 1,       // how many CI-duplicates were skipped
  "pool_size": 4      // total active-pool size after the write
}
```

Empty / non-string / whitespace-only entries in the input list are silently skipped (per the speaktube spec: "split on commas, trim whitespace, drop empties").

### `GET /api/preferences/keywords` (slice 11, 2026-05-17)

Returns the currently-active keyword pool - keywords POSTed since the most-recent `curator_playlist` composition (those haven't been "consumed" by a fire yet), OR (fallback) keywords from the trailing `curator_keyword_pool_fallback_seconds` window when no composition exists yet. Always 200:

```json
{ "keywords": ["Joinery", "public-domain noir"] }
```

Speaktube can render this as a "tomorrow's pool" preview list on the Discover view.

**Composer integration:** at AG-fire time, the dispatcher's `_maybe_apply_keyword_boost` hook reads the active pool and:
1. Boosts `interest_score` on candidates whose `title` contains any active keyword (case-insensitive substring match) by `curator_keyword_boost_amount` (default +0.2). Result clamped to `[0, 1]`. Stacks on top of topic-scoring.
2. Injects the keyword list into the composer prompt via the `$KEYWORD_POOL` placeholder, so the LLM sees the operator's recent interests explicitly AND sees boosted scores on matching items (defense in depth).

**Operator controls (3 settings):**
- `curator_keyword_boost_enabled` (bool, default True) - master switch
- `curator_keyword_boost_amount` (float, default 0.2, range 0.0-1.0) - boost magnitude per match
- `curator_keyword_pool_fallback_seconds` (int, default 86400 = 24h, range 3600-604800) - fallback window when no composition exists yet

The keyword IMMUTABLE schema (`_epoch`, `event_ts_iso`, `keyword`, `source`, `raw_request`) is additive-only.

### `POST /api/growth_dial`

Persists the BIPOLAR exploration knob value. Body:

```json
{ "value": -0.4, "set_at": "2026-05-17T09:14:22-07:00" }
```

`value` must be in `[-1.0, +1.0]` (bipolar per slice 8, 2026-05-17 - was `[0.0, 1.0]` through slice 7). Semantics:

* **-1.0** - maximum comfort, only channels the user watches a lot.
* **0.0** - balanced, mostly familiar with some exploration.
* **+1.0** - maximum expansion, heavy bias toward never-watched channels.

`set_at` is accepted but only used for logging - the persisted shape is "current value", not a history. Writes to `global_settings.yaml` under `curator_growth_dial`. The next composition reads the new value at run time. `set_at` is captured in the `config_changes` log so the audit trail records every adjustment.

**Migration note (one-time, slice 8 2026-05-17):** before slice 8, the endpoint accepted only `[0.0, 1.0]` with INVERTED semantic (0.0 = familiar, 1.0 = explore). Speaktube's slider sent `-1..+1` values; SpeakesQuery silently rejected the negative half so the left side of the slider had no effect. After slice 8, the slider's full range takes effect. Operators whose stored value was in `(0.0, 1.0]` will see it re-interpreted in the new bipolar scale (e.g. an old stored 0.3 was "slight familiarity"; in bipolar it's "moderate exploration"). Re-set via the slider or directly: `curl -X POST -d '{"value":-0.4}' .../api/growth_dial`. The IMMUTABLE journal preserves historical compositions' `growth_dial` field verbatim - those values reflect the operator's intent AT THE TIME, not the current scale. For analytics across the slice-8 boundary, treat compositions before 2026-05-17 as the old scale and after as bipolar.

## Storage layout

All curator data lives under `indexes/IMMUTABLE/` so the cleanup budget never evicts it. The decade-horizon design means viewing telemetry, reflections, and composed playlists are first-class long-tail data.

| Subdirectory | Source | Schema |
|---|---|---|
| `curator_telemetry/` | hourly speaktube pull + `POST /api/reflections` (via reflection_submit) | one row per playback / interaction event |
| `curator_reflections/` | `POST /api/reflections` + reflection_submit telemetry | one row per free-text note |
| `curator_playlist/` | nightly composition (future slice) | one row per item in a composed run |
| `curator_takeout/subscriptions/` | one-shot Takeout import | one row per subscribed channel |
| `curator_takeout/playlists_metadata/` | one-shot Takeout import | one row per playlist |
| `curator_takeout/playlist_videos/` | one-shot Takeout import | one row per video in a playlist |
| `curator_takeout/watch_history/` | one-shot Takeout import | one row per past watch |

All three log_writer-driven schemas (`curator_telemetry`, `curator_reflections`, `curator_playlist`) are **additive-only** - see CLAUDE.md "Do Not" pin. Removing a column breaks every historical query that touched it.

## Google Takeout bootstrap

The user's existing YouTube account state - subscriptions, history, playlists - lives in a Google Takeout export at `<project_root>/youtube_profile/` (gitignored). Run the one-shot CLI to convert it into queryable parquets:

```sh
python -m tools.curator_takeout_import                   # default paths
python -m tools.curator_takeout_import --root ~/yt/      # custom Takeout root
python -m tools.curator_takeout_import --json            # JSON summary for piping
```

The CLI parses four artifacts:

1. **`subscriptions/subscriptions.csv`** → `indexes/IMMUTABLE/curator_takeout/subscriptions/*.parquet`
2. **`playlists/playlists.csv`** → `playlists_metadata/`
3. **`playlists/<NAME>-videos.csv`** (one per playlist) → `playlist_videos/` (each video row carries its source playlist's name)
4. **`history/watch-history.html`** → `watch_history/`

Missing artifacts are skipped with a warning, not a failure. The watch-history parser handles entries with missing channels (private/deleted videos) and emits a row with empty channel fields rather than dropping the entry.

Re-running the importer is safe - each invocation writes a new `<epoch>_<uuid>.system4.system4.parquet`, so historical imports remain alongside fresh ones. Cleanup never touches IMMUTABLE.

### Watch-history timestamp parsing

Takeout writes dates as `May 13, 2026, 8:28:07 PM PDT`. The importer maps known TZ abbreviations (US + EU + UK) to UTC offsets and emits a correct UTC `_epoch`. Unknown abbreviations land with the parsed wall-clock ISO + the raw `tz_abbrev` preserved for forensics, and `_epoch` falls back to now-UTC (i.e. the row still ingests, just with a less-useful epoch - operators can re-parse via SPQL if a critical abbrev needs adding).

## Hourly telemetry pull

The speaktube sidecar appends one JSON object per line to `${TELEMETRY_DIR}/YYYY-MM-DD.jsonl` and exposes those files at `<base>/api/telemetry/<date>.jsonl`. SpeakesQuery pulls them via a standard sandboxed ingestion script.

* **Script**: [`script_library/scripts/curator_telemetry_pull.json`](../../script_library/scripts/curator_telemetry_pull.json)
* **Default cron**: `5 * * * *` (hourly at 5 past)
* **Output dir**: `indexes/IMMUTABLE/curator_telemetry/`
* **Lookback**: 3 days (paranoid catch-up; dedup on `event_ts_iso + event_type + video_external_id`)
* **Allowlist**: `localhost` (the default speaktube host) is on `allowed_api_domains` by default; add your speaktube host's name if it runs on another machine

Deploy via the Ingestions page. The `api_url` field on the deployed task accepts an override if speaktube moves to a different host. Day-1 no-op (returns empty DataFrame, no failure) until speaktube ships `/api/telemetry/<date>.jsonl` - that's deliberate so the cron can be scheduled before both sides are wired.

### Event types

Every event line carries the common envelope:

```json
{
  "event_type": "play_start",
  "event_ts": "2026-05-16T09:14:22-07:00",
  "video_external_id": "dQw4w9WgXcQ",
  "chosen_by": "curator",
  "run_date": "2026-05-16",
  "position": 3
}
```

| `event_type` | Extra fields | Notes |
|---|---|---|
| `play_start` | - | first time the video starts playing on this load |
| `play_end` | `watched_seconds`, `total_seconds` | natural completion |
| `skip` | `watched_seconds`, `total_seconds` | route change / unload before completion |
| `rate` | `rating` (1-9) | Watch-view rating buttons |
| `mark_junk` | `reason` (free text, optional) | Junk button |
| `reflection_submit` | `kind`, `content` | per-video reflection (also writes to `curator_reflections`) |
| `manual_search` | `query` | TopNav search submit |
| `impression` | `position`, `slot_kind` | coming soon - viewport intersection |

The ingestion script preserves any unknown field in the `raw_json` column verbatim, so a renderer-side schema extension on speaktube doesn't require any change here.

## Candidate ingestion (slice 1.5)

The Takeout bootstrap is historical-only - your past watches and subscriptions, not videos to recommend today. **Slice 1.5** (2026-05-16) ships the first candidate-discovery feed: a sandboxed ingestion script that pulls fresh uploads from your subscribed channels via YouTube's free public RSS feeds.

* **Script**: [`script_library/scripts/curator_youtube_rss_pull.json`](../../script_library/scripts/curator_youtube_rss_pull.json)
* **Default cron**: `0 */6 * * *` (4 runs/day)
* **Output dir**: `indexes/IMMUTABLE/curator_candidates/`
* **Allowlist**: `www.youtube.com` is added to `allowed_api_domains` by default
* **No auth**: YouTube RSS is unauthenticated public

### How priority works

The script reads `indexes/IMMUTABLE/curator_takeout/subscriptions/` and joins with `curator_takeout/watch_history/` to compute a per-channel watch-count score. Channels you've watched 50+ times get queried first; channels you subscribed to but never watched get queried last. Per-run the script processes the top `MAX_CHANNELS_PER_RUN` (default 40, edit the script to bump) - with 4 runs/day this is ~160 channels/day, full coverage of a 634-channel subscription list in roughly 4 days. **The signal-rich subset always stays current** regardless of total subscription count.

### Candidate row schema

Every row written under `indexes/IMMUTABLE/curator_candidates/` has the following canonical shape - additive-only, so future non-YouTube sources (Archive.org, PBS Frontline, Vimeo, etc.) can land in the same index without breaking the composer or any historical SPQL query:

| Column | Notes |
|---|---|
| `_epoch` | Video publication time (Unix seconds) - for `earliest=` / `latest=` filters |
| `discovered_at_epoch` | When this script run saw it - for freshness filters |
| `source` | `youtube_rss` (slice 1.5), `topic_search:youtube:<cluster_id>` (slice 3b), `archive_org` (slice 7), `youtube_rss_info` / `archive_org_info` / `topic_search_info` (per-source info rows for empty-state fallbacks). New sources land their own string. |
| `video_external_id` | Platform video id (YouTube 11-char base64url) |
| `video_url` | yt-dlp-resolvable URL (load-bearing - speaktube feeds this to the player) |
| `title` | |
| `channel_id` / `channel_name` / `channel_url` | |
| `published_iso` | Raw ISO from the feed |
| `description` | Snippet, truncated to 1000 chars |
| `duration_seconds` | Null for RSS (the feed doesn't carry it; will be populated by future sources or a Phase 6.x enrichment pass) |
| `thumbnail_url` | Image URL from `<media:thumbnail url=…>`; empty string when the feed omits one. Threaded to the playlist + `/api/playlist/today` so the speaktube player renders card images without client-side synthesis. Added 2026-05-17 (slice 4). |
| `raw_blob` | Full original `<entry>` markup, truncated to 5000 chars - forward-compat for fields YouTube might add |

### Operator quick reference

```sh
# Deploy via the Ingestions page (Library → "Curator YouTube RSS pull" → Deploy).
# Default settings are fine.

# Query what's been discovered
# index="indexes/IMMUTABLE/curator_candidates/*" | stats count by source
# index="indexes/IMMUTABLE/curator_candidates/*" | stats count by channel_name | sort -count | head 20
# index="indexes/IMMUTABLE/curator_candidates/*" | where _epoch >= relative_time(now, "-1d") | head 100
```

The script does NOT dedup against prior runs - every discovery emits a new row. The composer slice will dedup by `video_external_id` at composition time. This keeps the script idempotent at the ingestion-pipeline level and preserves a full "when did we first see this?" audit trail (the difference between `_epoch` and `discovered_at_epoch` tells you the latency between publish and discovery).

### Multi-source rollout (slice 7, 2026-05-17): Archive.org (Tier 1)

Speaktube's request 8 asked for a multi-source rollout beyond YouTube. **Archive.org is the Tier 1 starting point** - best ethos fit (no engagement algorithm, public-domain long tail), no auth, no rate limit, native yt-dlp support.

`curator_archive_org_pull` (sandboxed Python, default cron `0 4 * * *`) hits the public `advancedsearch.php` endpoint with `mediatype:movies AND sort[]=publicdate desc`, parses the JSON response, and emits canonical 14-column candidate rows with:

| Field | Value |
|---|---|
| `source` | `archive_org` |
| `video_external_id` | Archive.org identifier (e.g. `charade_1963`, `MIT_Open_Courseware_lecture_42`) |
| `video_url` | `https://archive.org/details/<identifier>` (yt-dlp resolves natively) |
| `channel_id` | `archive_org:<creator_slug>` - namespaced so it doesn't collide with YouTube `UC...` IDs |
| `channel_name` | Item's `creator` field (free-text, may be empty) |
| `thumbnail_url` | `https://archive.org/services/img/<identifier>` (always populated) |
| `duration_seconds` | Parsed from Archive.org's `runtime` string (`HH:MM:SS` / `MM:SS` / seconds) - null when unparseable |
| `published_iso` | Archive.org's `publicdate` (when the item was added to the archive), falling back to the item's `date` field |

Operator-tunable in the script: `ARCHIVE_ORG_QUERY` (default `mediatype:movies`) and `MAX_ITEMS_PER_RUN` (default 40). For narrower curation, try `collection:opensource_movies AND mediatype:movies` or specific filters like `subject:noir`.

**Scoring behavior in the composer's feeder:**
Archive.org rows enter via a third `| append` branch in `curator_scored_candidates_today.yaml`. All archive.org rows score `interest_score=0.0` and `growth_score=1.0` at the feeder layer because the user has no YouTube watch_history match for them. The composer's `apply_topic_scoring: true` hook then **re-scores `interest_score` by topic-cosine-similarity** at AG-fire time, so archive.org items still surface in the user's topical clusters when relevant (an MIT SICP lecture matches a "computer science" cluster; a public-domain noir matches a "classic film" cluster).

The cross-source canonical-schema drift guard lives in `tests/test_curator_slice3b_topic_search.py::test_emits_same_canonical_columns_as_slice_1_5` - it parses all three ingestion scripts' `EXPECTED_COLUMNS` literals and asserts they match. Adding a future source (DailyMotion, Vimeo, Tubi, etc.) means extending that test's slice-list AND the feeder's append chain.

```sh
# Deploy via the Ingestions page (Library → "Curator Archive.org pull" → Deploy).

# Query what archive.org has surfaced today
# index="indexes/IMMUTABLE/curator_candidates/*" | where source="archive_org" | stats count
# index="indexes/IMMUTABLE/curator_candidates/*" | where source="archive_org" | head 10 | table title, channel_name, duration_seconds, video_url
```

**Future sources (Tier 2/3) follow the same canonical-schema-additive pattern.** Each new source script writes to `indexes/IMMUTABLE/curator_candidates/` with its own `source` string and the same 14-column shape; one `| append` line in the feeder picks it up. Tier 2 (DailyMotion, Vimeo, Tubi/Pluto/Roku) and Tier 3 (alt-tech) are deferred until Archive.org reaches steady-state and slop-scoring has been tuned per-source.

## Composition (slice 2)

**Slice 2 (2026-05-16)** ships the daily playlist composer as an alert group.

### Components

| Artifact | Path |
|---|---|
| Scored-candidates feeder (SPQL) | [`default_saved_searches/curator_scored_candidates_today.yaml`](../../default_saved_searches/curator_scored_candidates_today.yaml) |
| Composer boilerplate prompt | [`boilerplate_prompts/curator_compose_playlist.yaml`](../../boilerplate_prompts/curator_compose_playlist.yaml) |
| Default alert group | [`default_alert_groups/curator_playlist_composer.yaml`](../../default_alert_groups/curator_playlist_composer.yaml) |
| Dispatcher hook | `_parse_playlist_block` / `_log_playlist_items` / `_extract_and_log_playlist` in [`alert_groups/dispatcher.py`](../../alert_groups/dispatcher.py) |

### Scoring SPQL

The feeder joins candidates with watch_history and emits three scores per row:

```spql
index="indexes/IMMUTABLE/curator_candidates/*"
| where discovered_at_epoch >= relative_time("-2d")
| join channel_id [search index="indexes/IMMUTABLE/curator_takeout/watch_history/*" | where channel_id != "" | stats count as watch_count by channel_id]
| eventstats max(watch_count) as max_watch
| eval interest_score=round(watch_count / max_watch, 3)
| eval growth_score=round(1.0 - interest_score, 3)
| eval slop_score=if_(match(lower(title), "won.?t believe|shocking|insane|gone wrong|you.?ll never"), 0.8, 0.1)
| table video_external_id, video_url, title, channel_name, channel_id, watch_count, interest_score, growth_score, slop_score, _epoch, published_iso, thumbnail_url, source
| sort -interest_score
| head 100
```

* **interest_score** - channel's share of the user's all-time watch count (1.0 = top-watched)
* **growth_score** - inverse of interest (channels rarely watched = high exploration potential)
* **slop_score** - heuristic on the title; 0.8 if clickbait pattern matches, 0.1 otherwise

### How it dispatches

1. Cron fires (daily 05:00 America/Los_Angeles per the default schedule)
2. The feeder runs → 100 scored candidate rows
3. Prompt is assembled: composer template + scored candidates as a serialized table block
4. **If `dry_run: true` on the AG YAML**: dispatcher logs the prompt at INFO and returns `status=dry_run`. **No LLM call, no cost.** Operator inspects the logs via `index="indexes/logs/alert_groups/*"`.
5. **If `dry_run: false`**: dispatcher calls Claude (Sonnet 4.6 for slice 2 MVP; local 122B routing is Phase 6.x)
6. LLM returns a JSON object: `{run_date, growth_dial, theme, items: [...]}` in a fenced block
7. Dispatcher's `_extract_and_log_playlist` parses + validates + writes one row per item via `log_curator_playlist_item` to `indexes/IMMUTABLE/curator_playlist/`
8. `GET /api/playlist/today` reads the most-recent run and serves it to speaktube

### Output kind = playlist (dispatcher discriminator)

The AG YAML field `output_kind: playlist` is the contract that tells the dispatcher to route through `_extract_and_log_playlist` instead of the default `_extract_and_log_picks`. Bare AGs (no `output_kind`) continue to write to `ag_picks` per the OEB pattern. This is the only AG with `output_kind != "picks"` today - future trading / monitoring / etc. AGs can declare their own output_kind values for their own writers.

### Per-AG `dry_run: true` (safety gate)

Set `dry_run: true` in the AG YAML to fire the full feeder loop + prompt build but **skip the LLM call**. The dispatcher logs the would-be prompt at INFO so the operator can paste it into Claude.ai manually OR just eyeball the structure. Money-leak canary in `tests/test_curator_composer_slice2.py::TestDryRunMoneyLeakCanary` pins this - `dry_run: true` MUST produce zero billable LLM calls.

Flip to `dry_run: false` via the UI (Alert Groups → Edit → Dry Run toggle) or by editing the YAML directly after eyeballing 3-5 days of dry-run output.

### Operator quick reference

```sh
# Manually trigger the composer (e.g. for a one-off test after redeploy)
curl -X POST http://localhost:5111/api/alert-groups/curator_playlist_composer/run

# Check what got composed
curl http://localhost:5111/api/playlist/today | jq .

# Inspect raw playlist rows via SPQL
# index="indexes/IMMUTABLE/curator_playlist/*" | sort -composed_at_iso | head 20

# Inspect dry-run prompts in the AG log
# index="indexes/logs/alert_groups/*" | where group_name="curator_playlist_composer" | sort -_epoch | head 10
```

## Operator quick reference

```sh
# Verify the endpoints respond (from any LAN host)
curl -s http://localhost:5111/api/playlist/today   # 404 until composition runs
curl -s http://localhost:5111/api/dignity/today    # 200 + null until plays land
curl -s -X POST -H 'content-type: application/json' \
  -d '{"value":-0.4}' http://localhost:5111/api/growth_dial  # bipolar slice 8 (was 0..1)

# Run the Takeout import (one-shot)
python -m tools.curator_takeout_import

# Query telemetry via SPQL
# (in the SpeakesQuery query box)
# index="indexes/IMMUTABLE/curator_telemetry/*" | stats count by event_type, chosen_by
# index="indexes/IMMUTABLE/curator_takeout/subscriptions/*" | stats count
# index="indexes/IMMUTABLE/curator_takeout/watch_history/*" | stats count by channel_name | sort -count | head 20
```

## Test gates

The slice is pinned by [`tests/test_curator_speaktube_slice1.py`](../../tests/test_curator_speaktube_slice1.py):

* **Frozen-column snapshots** for all three log_writer schemas (additive-only enforcement).
* **IMMUTABLE_CATEGORIES membership** so the three curator categories never get demoted to the cleanup-eligible logs tree.
* **Endpoint contract tests** for the four routes - happy path, validation errors, date filtering, empty-state semantics.
* **Config-leak canary** - `GET /api/playlist/today` and `GET /api/dignity/today` must never call `AlertGroupStore.save_group` / `update_group`, same defensive pattern as Phase 3 slice 9.
* **Takeout import unit tests** - per-parser plus end-to-end against a synthetic fixture.
* **speaktube-host allowlist drift guard** so a future cleanup of `allowed_api_domains` doesn't silently lock the curator out.

The telemetry-pull ingestion script is also pinned by `tests/test_script_library.py` (no-auth registry coverage, sandbox compilation gate, mock URL round-trip).

---

## Slice 3 - Topic-driven discovery (2026-05-16)

**The bootstrap-bias unlock.** Slice 1.5 pulled candidates from your existing YouTube subscriptions; slice 2 scored them by raw `watch_count`. Both layers were anchored to YouTube's own curation - which is exactly the bias the curator is meant to escape (memory: project_curator_vision_2026_05_16). Slice 3 rewires the *scoring* layer so candidates rank by **topical similarity to clusters of titles you actually watched**, not by how often you've returned to a channel.

The slice ships in two parts. **Slice 3a (this release)** ships the scoring rewrite + dispatcher hook + composer prompt. **Slice 3b (follow-up)** adds a yt-dlp topic-search ingestion script so the candidate pool draws from channels *outside* your subscriptions - that's the breadth piece. After 3a alone the composer re-scores the existing 463-candidate pool by topic; after 3b you see new channels surfaced.

### The pieces

| Surface | Where | Purpose |
|---|---|---|
| [`analyzers/topic_vectors.py`](../../analyzers/topic_vectors.py) | new module | `compute_topic_snapshot` (KMeans over recency-weighted history embeddings), `score_candidates_against_snapshot` (cosine to nearest centroid), `label_clusters_with_llm` (optional LLM labels via cost-cascade), serialisation helpers, `load_latest_snapshot`. |
| `curator_topic_snapshots` log schema | [`functionality/log_writer.py`](../../functionality/log_writer.py) | One row per cluster per snapshot in `indexes/IMMUTABLE/curator_topic_snapshots/`. Forever-data; additive-only schema. |
| [`tools/curator_topic_snapshot_refresh`](../../tools/curator_topic_snapshot_refresh.py) | CLI | One-shot bootstrap + tuning loop. Same code path the engine job uses. |
| `_schedule_topic_snapshot_refresh` | [`scheduled_input_engine/engine.py`](../../scheduled_input_engine/engine.py) | Weekly engine-scheduled refresh, gated by `curator_topic_snapshot_refresh_enabled` (default OFF). |
| `_maybe_apply_topic_scoring` | [`alert_groups/dispatcher.py`](../../alert_groups/dispatcher.py) | Per-AG post-feeder hook. When the AG sets `apply_topic_scoring: true`, every feeder DataFrame gets `interest_score` / `topic_cluster_id` / `topic_label` / `topic_similarity` columns appended *before* serialisation into the prompt. |
| `curator_playlist_composer` opt-in | [`default_alert_groups/curator_playlist_composer.yaml`](../../default_alert_groups/curator_playlist_composer.yaml) | Ships with `apply_topic_scoring: true` + a rewritten prompt explaining the new scoring semantics + a 30% single-channel cap. |
| `llamacpp-qwen3-32b-q4km` model | [`default_models/llamacpp-qwen3-32b-q4km.yaml`](../../default_models/llamacpp-qwen3-32b-q4km.yaml) | Generic template for a self-hosted Qwen3 32B on llama.cpp (LM Studio-compatible). Operator points `endpoint` at their LAN host post-deploy. |

### Bootstrap (operator workflow)

After deploying slice 3 (`git pull` on the host), **rebuild the Docker
image so the new tools/, analyzers/, and Python deps land in the
container**:

```sh
cd ~/speakesQuery && ./install.sh
```

This is required on the FIRST deploy of slice 3 (and on any slice that
adds Python deps to requirements.txt, e.g. yt-dlp for slice 3b). After
slice 3a's `8669421` hotfix, `tools/` is bind-mounted into the container
so future tools/* iterations only need `docker compose restart
speakesquery-desktop` - but the bind-mount itself is part of the
docker-compose.yml change that requires the initial rebuild.

Then bootstrap:

```sh
# 1. (Optional since the 2026-06-07 repoint.) The default labeling
#    model is now `llamacpp-qwen35-122b-a10b`, which already ships
#    pointed at your llama.cpp server (see `models/llamacpp-qwen35-122b-a10b.yaml`) - edit the endpoint if needed. The
#    step below only applies if you instead use the generic 32B
#    template and need to point it at your LAN host. The model registry
#    has no UI page yet (slice 3.5 follow-up); edit the bind-mounted
#    YAML directly. models/ is bind-mounted from the host per
#    desktop_app/docker-compose.yml so this propagates into the
#    container without a rebuild.
sed -i 's|http://localhost:8080/v1|http://<your-llama-host>:8080/v1|' \
    models/llamacpp-qwen3-32b-q4km.yaml
docker compose -f desktop_app/docker-compose.yml restart speakesquery-desktop

# 2. Build the first topic snapshot from your watch history.
#    Run via `docker exec` because pandas/sklearn/sentence-transformers
#    live inside the container, not the host Python.
#    Takes ~30-60s on a typical ~5000-row Takeout corpus.
#    The CLI defaults to --title-col=video_title to match the Takeout
#    importer's schema (slice 3a hotfix 8669421, 2026-05-17).
docker exec speakesquery-desktop python -m tools.curator_topic_snapshot_refresh

# 3. Inspect the clusters that came back. Re-run with different K
#    or different decay if the grain feels off:
docker exec speakesquery-desktop python -m tools.curator_topic_snapshot_refresh \
    --n-clusters 12 --decay-lambda-days 365

# 4. Preview labels WITHOUT spending (money-leak gate; uses
#    placeholders, zero LLM calls):
docker exec speakesquery-desktop python -m tools.curator_topic_snapshot_refresh \
    --dry-run-labels

# 5. Once happy, enable the weekly refresh job in Settings →
#    Curator / speaktube → "Topic Snapshot Refresh".

# 6. The composer AG will pick up the new scoring on its next fire
#    (cron: daily at 05:00 PT). dry_run stays ON by default - flip
#    it OFF after eyeballing one composed prompt.
```

**Future iteration loop** (after the slice 3a bind-mount hotfix lands):
when you `git pull` a CLI-only fix to `tools/*.py`, just restart the
container - no rebuild needed.

```sh
cd ~/speakesQuery && git pull
docker compose -f desktop_app/docker-compose.yml restart speakesquery-desktop
docker exec speakesquery-desktop python -m tools.curator_topic_snapshot_refresh
```

(Changes to `requirements.txt`, `Dockerfile`, or other top-level Python
deps still need `./install.sh`. The bind-mount only covers `tools/`.)

### Settings (six new keys, all five-place drift-guarded)

| Key | Default | Purpose |
|---|---|---|
| `curator_topic_snapshot_refresh_enabled` | `false` | Master switch for the engine job. The CLI works regardless. |
| `curator_topic_snapshot_refresh_interval_hours` | `168` | Weekly cadence. Topic style is slow-evolving - daily is overkill. |
| `curator_topic_n_clusters` | `10` | KMeans K. Capped at `len(history)` at compute time. |
| `curator_topic_decay_lambda_days` | `180` | Recency half-life (≈6 months). Higher = older watches matter more. |
| `curator_topic_label_model_id` | `llamacpp-qwen35-122b-a10b` | Registry id for the labeling LLM. Defaults to a self-hosted Qwen3.5-122B-A10B on llama.cpp ($0, endpoint from the model registry); the older `llamacpp-qwen3-32b-q4km` (32B) remains as the rollback, or swap for `claude-haiku-4-5-20251001` for a cloud fallback. |
| `curator_topic_label_max_cost_usd` | `0.05` | Hard ceiling on cumulative labeling cost per refresh. $0 for the local default; bump when using a cloud model. |

### How the dispatcher hook degrades gracefully

The slice-3 hook is opt-in and defence-in-depth:

| Condition | Behaviour |
|---|---|
| `apply_topic_scoring` absent or `false` | Hook is a no-op. AG behaves identically to slice 2. |
| `df is None` (feeder failed) | No-op, returns `None`. No crash. |
| No snapshot persisted yet | Returns df unchanged with a `[!]` warning. The composer prompt is written to tolerate unscored rows. |
| `score_candidates_against_snapshot` raises | Returns df unchanged with a `[!]` warning citing the snapshot id + error. Dispatch continues. |
| Happy path | df returned with `interest_score` / `topic_cluster_id` / `topic_label` / `topic_similarity` columns appended. |

### Test gates (slice 3a)

[`tests/test_curator_topic_vectors_slice3.py`](../../tests/test_curator_topic_vectors_slice3.py) pins **27 invariants** across six areas:

* **Frozen-column drift guard** for the new `curator_topic_snapshots` schema (additive-only forever).
* **IMMUTABLE_CATEGORIES membership** so the snapshot timeline is never garbage-collected.
* **Unit tests** for `compute_topic_snapshot`, `score_candidates_against_snapshot`, `_clean_label`, JSON serialisation round-trip.
* **MONEY-LEAK CANARY** - `label_clusters_with_llm(dry_run=True)` makes zero `analyzers.llm_router.call_llm` invocations. Pinned by an `AssertionError`-raising mock; if a future refactor accidentally bypasses the short-circuit, this test fails loudly. Same pattern as `tests/test_curator_composer_slice2.py::TestDryRunMoneyLeakCanary`.
* **Dispatcher hook drift guard** - flag-off, df-none, no-snapshot, and happy-path cases all pinned.
* **Composer YAML + prompt drift guard** - pins `apply_topic_scoring: true`, the `topic_cluster_id` / `topic_label` markers in the prompt, and the literal `30%` cap.
* **AlertGroupStore allowlist drift guard** - `apply_topic_scoring` and `topic_scoring_title_col` round-trip through PUTs (same drift class as slice 2's `dry_run` + `output_kind`).

### Slice 3b - yt-dlp topic-search (2026-05-16)

The **breadth** companion to slice 3a's scoring rewrite. With 3a, the composer scored the *existing* 463-candidate pool (still 3 channels). 3b adds a new candidate source that doesn't ask "what are you subscribed to?" - it asks "what topics do you care about?" - and uses yt-dlp's search to pull videos from channels you've never subscribed to.

| Surface | Where | Purpose |
|---|---|---|
| `yt-dlp` Python dep | [`requirements.txt`](../../requirements.txt) | Ships in the Docker image after `./install.sh` rebuild. |
| [`curator_topic_search_pull_pro`](../../script_library/scripts/curator_topic_search_pull_pro.json) | `_pro`-tier ingestion script | Reads the latest topic snapshot (via DuckDB, zero project-internal imports), picks the top 8 clusters by recency-weighted importance, runs `ytsearch10:<cluster_label>` per cluster, lands canonical 13-column rows in `indexes/IMMUTABLE/curator_candidates/` with `source="topic_search:youtube:<cluster_id>"`. |
| Tests: [`tests/test_curator_slice3b_topic_search.py`](../../tests/test_curator_slice3b_topic_search.py) | 7 happy-path + drift-guard tests | Synthetic snapshot on disk + mocked `yt_dlp.YoutubeDL`; pins cross-source schema agreement between slice 1.5 and slice 3b (so the composer can read both with one SPQL query). |

### How slice 3b uses the slice 3a snapshot

1. Reads the *most recent* `snapshot_epoch` row group from `indexes/IMMUTABLE/curator_topic_snapshots/*.parquet` via DuckDB.
2. Sorts clusters by `weight` (recency-weighted importance) DESC, takes top 8.
3. For each cluster:
   * If the label is a real LLM-generated string (not "Cluster N", not "(dry-run)", not "(budget capped)"), use it as the search query.
   * Otherwise, fall back to the first exemplar title from `exemplar_titles_json`. This means a snapshot built with `--dry-run-labels` (no real labels) still produces candidates - the fallback keeps the pipeline alive.
4. Runs `yt_dlp.YoutubeDL.extract_info(f"ytsearch10:{query}", download=False)` with `extract_flat='in_playlist'` (avoids per-video metadata fetches; one HTTP round-trip per cluster).
5. Each result becomes one canonical-13-column row with `source="topic_search:youtube:<cluster_id>"` and a `raw_blob` JSON carrying cluster attribution + view count + live-status for downstream debugging.

### Operator workflow

```sh
# After slice 3b deploy (./install.sh on the SpeakesQuery host):

# 1. Ensure a snapshot exists (from slice 3a bootstrap). If not, build one:
docker exec speakesquery-desktop python -m tools.curator_topic_snapshot_refresh

# 2. Deploy the new ingestion script via the Ingestions page → Add From Library
#    → "Curator topic-search pull pro". Suggested cron: 0 4 * * * (4am daily,
#    so candidates land before the composer's 05:00 fire).

# 3. Click "Run now" once to validate it lands rows. Check via SPQL:
#    index="indexes/IMMUTABLE/curator_candidates/*"
#      | search source="topic_search:youtube:*"
#      | stats count by source, channel_name | sort -count
#    You should see candidates from channels you DON'T subscribe to.

# 4. The composer's next 05:00 fire (or manual Run) sees a candidate
#    pool that now includes topic-search results alongside subscription
#    RSS results. The dry-run prompt should reflect new channels.
```

### What changes in the composer dry-run after 3b

Slice 3a alone re-scored the existing pool (same 3 channels, reordered). Slice 3b adds new candidates - the next dry-run should show:

* New channels appearing in the candidate pool (anything matching your topic clusters that yt-dlp's search surfaces).
* The composer's 30% single-channel cap now has more channels to spread across.
* `raw_blob` carries the `cluster_label` + `search_query` that surfaced each candidate, so you can audit which topic produced which video.

### Cross-source schema contract

Both slice-1.5 and slice-3b emit the same 13-column candidate schema. This is the **canonical multi-source schema** (memory: `reference_canonical_schema_for_multi_source_ingestion`) - every future source (PeerTube, Archive.org, podcast RSS, Vimeo, etc., all yt-dlp extractors) MUST emit the same column set with its own `source=` discriminator. The composer reads the entire pool with one SPQL query; sources are pluggable.

Pinned by `tests/test_curator_slice3b_topic_search.py::test_emits_same_canonical_columns_as_slice_1_5` - extracts `EXPECTED_COLUMNS` from both scripts via regex and asserts set equality. If a future source adds or removes a column, this test fails loudly until both scripts are updated in lockstep.
