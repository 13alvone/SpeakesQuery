# speaktube ↔ SpeakesQuery: post-launch deltas (slices 4–12)

**Authored 2026-05-17 by the SpeakesQuery side.** This is a consolidated handoff for the speaktube team covering every change SpeakesQuery shipped between commits `6174cd3` (the slice-3 close-out) and `bf3c51c` (slice 12). Nine slices, all on `origin/main`, all tied to specific asks from `SPEAKESQUERY_REQUESTS.md`.

Read the top-level summary table first to triage. Per-slice sections below have the actionable details for the speaktube renderer.

---

## Summary table

| Slice | Commit | speaktube req | Headline | What speaktube needs to know |
|---|---|---|---|---|
| 4 | `22bd689` | #1, #2 | `thumbnail_url` + `published_at` end-to-end | TWO new keys on the `video` object. Both always present; empty string is the load-bearing "we don't have one" signal. |
| 5 | `96e93b7` | #4, #9 | Dispatcher dedup + position renumber | `position` is now guaranteed 1-indexed unique sequential. Defensive client-side `dedupeByExternalId` + `idx + 1` rank rendering remain harmless but redundant. |
| 6 | `940e9c9` | #3 | Hybrid expansion to ~500 items | Playlists now default to ~500 items. Items past position ~20 have empty `rationale` - treat as compact cards / no disclosure. |
| 7 | `af59d06` | #8 Tier 1 | Archive.org candidates ingest | First non-YouTube source. `video.source = "archive_org"`, URLs are `https://archive.org/details/<id>` (yt-dlp resolves natively). Operator must deploy the new ingestion script. |
| 8 | `7c2dfb1` | #6 | **Bipolar `growth_dial` −1..+1** | Range changed from `[0, 1]` to `[-1, +1]`. Stored values previously in `(0, 1]` are now interpreted in the new bipolar scale. Default changed from `0.15` to `-0.7` (same "mostly familiar" intent). |
| 9 | `2c255bd` | #5 | Channel-diversity cooldown | Bulk-fill respects 10%-per-channel cap + max 3-per-10-position window. Speaktube's defensive client-side reorder for diversity is now redundant. |
| 10 | `23953a3` | #12 + **bug fix** | Thin-history aggressive discovery + dial-injection fix | TWO new top-level response keys: `growth_dial_stored` + `thin_history_active`. **AUDIT FINDING**: prior to slice 10, the operator's slider had ZERO effect on composition. Fixed now. |
| 11 | `db63657` | #10 | Keyword preferences endpoint | TWO new endpoints: `POST /api/preferences/keywords` + `GET /api/preferences/keywords`. Storage forever; "active pool" semantics reset at composer fire. |
| 12 | `bf3c51c` | #11 | Cross-source search endpoint | ONE new endpoint: `GET /api/search?q=...`. Same JSON shape as `/api/playlist/today` so the renderer reuses one code path. |

---

## Production deploy (operator-side, once)

After `git pull` on `the SpeakesQuery host`:

```bash
# 1. Pull + install
cd /path/to/speakesQuery
./install.sh

# 2. Container restart picks up:
#    - new allowlist entry (archive.org)
#    - new settings + validators
#    - new REST endpoints
#    - new dispatcher hooks
#    - 3 new IMMUTABLE schemas
#    - new ingestion script file in script_library/

# 3. Deploy the new Archive.org ingestion via the Ingestions page:
#    Library → "Curator Archive.org pull" → Deploy.
#    Default cron 0 4 * * * is fine. (Or trigger Run Now for an
#    immediate first-data probe.)

# 4. TWO PUTs to push runtime updates that survive across operator
#    customizations:
#    - Composer prompt (slices 4, 5, 8, 9, 10, 11 all added prompt
#      rules + placeholders):
curl -X PUT http://localhost:5111/api/alert-groups/curator_playlist_composer \
     -H 'Content-Type: application/json' \
     -d "$(python3 -c "import yaml,json; d=yaml.safe_load(open('default_alert_groups/curator_playlist_composer.yaml').read()); d.pop('created_at',None); d.pop('updated_at',None); print(json.dumps(d))")"

#    - Composer feeder (slice 7 added archive.org append branch):
curl -X PUT http://localhost:5111/api/ss/curator_scored_candidates_today \
     -H 'Content-Type: application/json' \
     -d "$(python3 -c "import yaml,json; d=yaml.safe_load(open('default_saved_searches/curator_scored_candidates_today.yaml').read()); d.pop('created_at',None); d.pop('updated_at',None); print(json.dumps(d))")"

# 5. Next 05:00 PT composer fire → playlist exhibits all deltas at once.
#    Sample check:
curl -s http://localhost:5111/api/playlist/today | jq '{
  run_date,
  growth_dial,
  growth_dial_stored,
  thin_history_active,
  theme,
  item_count: (.items | length),
  first_three: [.items[:3] | .[] | {
    position, slot_kind, rationale,
    channel: .video.channel_name,
    thumbnail: .video.thumbnail_url,
    published_at: .video.published_at,
    source: .video.source
  }]
}'
```

---

## Per-slice speaktube-actionable deltas

### Slice 4 (`22bd689`) - `thumbnail_url` + `published_at`

**What changed (SpeakesQuery side):**
- `curator_candidates` ingestion row gained `thumbnail_url`. Both YouTube RSS (`<media:thumbnail url=…>`) and Archive.org (`services/img/<id>`) populate it.
- `curator_playlist` parquet row gained `thumbnail_url` + `published_at` (additive, schema-frozen).
- `/api/playlist/today` `video` object gained both fields.
- Composer prompt instructs the LLM to copy both verbatim from the candidate row.

**What speaktube needs to UPDATE:**
- The render path can stop synthesizing thumbnail URLs client-side for YouTube items - read `video.thumbnail_url` directly. Fall back to synthesis only when the field is empty string.
- The publication-date sort default now works against `video.published_at` (when populated). Falls back to curator order when empty.
- **No breaking change** - fields are always present (empty string is the load-bearing "no value" signal, never null, never key-missing).

**What stays the same:**
- All other `video` fields (`external_id`, `url`, `title`, `channel_name`, scores) unchanged.

---

### Slice 5 (`96e93b7`) - Dispatcher dedup + position renumber

**What changed:**
- `_parse_playlist_block` runs a post-pass that (a) drops duplicate `external_id`s (keep-first), (b) renumbers `position` to 1-indexed sequential.

**What speaktube needs to UPDATE:**
- Nothing required - your defensive `dedupeByExternalId` pass + `idx + 1` rank rendering keep working harmlessly. You CAN remove them in a future refactor: the server now guarantees both invariants.

**Composer prompt also tightened** to disambiguate the 10%-per-channel rule (see slice 9).

---

### Slice 6 (`940e9c9`) - Hybrid expansion to ~500 items

**What changed:**
- New setting `curator_playlist_target_count` (default 500). Composer's LLM still composes the top 10-20 items with full rationale + slot_kind; dispatcher then APPENDS additional rows from the scored-candidate pool to reach the target.
- Bulk-fill rows carry `rationale=""`, `slot_kind="main"`, scores from feeder. Per-fire LLM cost stays ~$0.02/day (we did NOT scale the LLM call to 500 items).

**What speaktube needs to UPDATE:**
- Render path should treat `rationale === ""` as "compact card / no disclosure". Cards with `rationale !== ""` get the full "Why this is here" disclosure. Both share the same JSON shape; differentiation is purely about UX density.
- The default playlist size jump from ~15 to ~500 is gradual on the operator side (they can dial back via `curator_playlist_target_count` if they prefer shorter playlists), but the player should be ready for 500-item lists in the typical case.

---

### Slice 7 (`af59d06`) - Archive.org Tier 1 ingestion

**What changed:**
- New sandboxed ingestion script `curator_archive_org_pull` pulls public-domain films + lectures from Archive.org's `advancedsearch.php` endpoint. Default cron `0 4 * * *`, 40 items/day.
- `curator_candidates` row schema unchanged (the same 14-column canonical shape from slice 1.5 + slice 4).
- New source enum value: `"archive_org"`.
- Composer feeder gained a third `| append` branch for `source="archive_org"`.

**What speaktube needs to UPDATE:**
- Render path may want to surface a "Source" pill on cards. Source enum values now in use: `"youtube_rss"`, `"topic_search:youtube:<cluster_id>"`, `"archive_org"`. The existing source-label mapping table in `Discover.vue` should already handle these - confirm.
- Archive.org URLs (`https://archive.org/details/<id>`) are yt-dlp-resolvable native; no special handling needed on the player side.

---

### Slice 8 (`7c2dfb1`) - **Bipolar `growth_dial` -1..+1** (BREAKING-ISH)

**What changed:**
- Validator range: `[0.0, 1.0]` → `[-1.0, +1.0]` (bipolar).
- Default value: `0.15` (slight familiarity in old semantics) → `-0.7` (slight familiarity in new bipolar semantics - linear remap, same operator intent).
- New semantics:
  - `-1.0` = max comfort, only channels the user watches a lot
  - `0.0` = balanced
  - `+1.0` = max exploration, never-watched channels
- All composer prompt + endpoint + UI + tests updated in lockstep.

**What speaktube needs to UPDATE:**
- The slider was already sending `-1..+1` values; SpeakesQuery was silently rejecting the negative half. **After slice 8, the full slider range takes effect.** The left half of the slider now works.
- If speaktube's slider UI was showing `0.0..1.0` numerically, change to `-1.0..+1.0`. The popover help text should match.

**Migration concern (operator-side):**
- Operators with an explicitly-stored value in `(0.0, 1.0]` (from a prior POST) will see it RE-INTERPRETED in the new bipolar scale (e.g. old `0.3` was "slight familiarity"; in bipolar it's "moderate exploration"). They should re-set via the slider once they notice the shift. The IMMUTABLE journal preserves historical compositions' `growth_dial` field verbatim - analytics across the slice-8 boundary must use `composed_at_iso` to choose interpretation.

---

### Slice 9 (`2c255bd`) - Channel-diversity cooldown

**What changed:**
- New settings: `curator_channel_cap_percent` (default 0.10) + `curator_channel_max_in_window` (default 3).
- Dispatcher's bulk-fill path enforces: no channel exceeds 10% of total playlist (50 items in a 500-item playlist), and no channel exceeds 3 items in any 10-position rolling window.
- LLM-curated portion (positions 1..~20) passes through unchanged - the composer prompt's tightened 10% rule keeps the LLM in check.

**What speaktube needs to UPDATE:**
- Your defensive client-side reorder for diversity is now redundant. You can keep it or remove it - server is doing it.
- The card render order is now guaranteed to be diverse by channel. If the renderer was working around clumpy ordering, that workaround is no longer needed.

**Graceful degradation:** when the candidate pool's diversity runs out near the tail, the algorithm ships a slight rule violation rather than truncating the playlist. The operator sees the warning in the server log; speaktube just gets a longer list.

---

### Slice 10 (`23953a3`) - Thin-history aggressive discovery + **DIAL-INJECTION BUG FIX**

**The audit caught a hidden bug.** Before slice 10, the composer prompt had hard-coded `"defaults to -0.7"` text - the operator's slider value was NEVER passed to the LLM. Your VM-round-4 report (`growth_dial: +0.40` with familiar channels still dominating) was exactly this. The dial had ZERO effect on composition.

**What changed (the fix + the feature):**
- Composer prompt now uses `$GROWTH_DIAL_VALUE` + `$THIN_HISTORY_ACTIVE` placeholders. Dispatcher substitutes them at AG-fire time. The LLM finally sees the current slider value.
- New thin-history detection: at dispatch time, sum `watched_seconds` from `indexes/IMMUTABLE/curator_telemetry/` for the trailing 30 days. Below threshold (default 5h), thin-history fires.
- When thin-history active, effective dial = `clamp(stored_dial + dial_bias, -1.0, +1.0)`. Default bias `+0.5` shifts `-0.7` → `-0.2`.
- `curator_playlist` parquet row gained `thin_history_active` column (additive).
- `/api/playlist/today` response gained TWO top-level fields:
  - `growth_dial_stored` - the operator's slider value as stored
  - `thin_history_active` - bool, was thin-history boosting active?

**What speaktube needs to UPDATE:**
- The slider's `growth_dial` value now actually affects composition. Re-test the slider workflow end-to-end.
- Render the divergence between `growth_dial` and `growth_dial_stored` when they differ - e.g. "Your slider is at -0.7 but we boosted to -0.2 because you've only watched 2 hours in the last 30 days." `thin_history_active === true` is the signal that explains the divergence.
- The renderer can display a "thin-history mode" badge when `thin_history_active === true`.

**3 new settings** (operator-tunable):
- `curator_thin_history_enabled` (bool, default True) - disable to use the dial value verbatim
- `curator_thin_history_threshold_seconds` (int, default 18000 = 5h)
- `curator_thin_history_dial_bias` (float, default 0.5)

---

### Slice 11 (`db63657`) - Keyword preferences endpoint + composer integration

**What changed:**
- TWO new REST endpoints:
  - `POST /api/preferences/keywords` - body `{ "keywords": ["k1", "k2"] }`. Each keyword writes to `indexes/IMMUTABLE/curator_keyword_prefs/` with `source="api_post"`. Case-insensitive dedup within-request AND against the active pool. Response: `{status, added, skipped, pool_size}`.
  - `GET /api/preferences/keywords` - returns `{"keywords": [...]}` (the active pool).
- New IMMUTABLE schema `curator_keyword_prefs` (forever-data, additive-only).
- Composer integration: dispatcher's `_maybe_apply_keyword_boost` hook reads the active pool at AG-fire time and boosts `interest_score` on candidates whose title contains an active keyword (case-insensitive substring) by `curator_keyword_boost_amount` (default +0.2). Stacks on topic-scoring.
- Composer prompt's `$KEYWORD_POOL` placeholder gets substituted with the comma-joined keyword list - LLM sees the operator's recent interests explicitly.

**What speaktube needs to UPDATE:**
- The new "Save for tomorrow" + "Also fold into tomorrow's pool" buttons can POST to `/api/preferences/keywords`. The endpoint accepts multiple POSTs in a single day and accumulates (case-insensitive dedup). Per the spec: "drains the input on success".
- Use `GET /api/preferences/keywords` to render the "tomorrow's pool: N keywords" badge. The endpoint returns `{ "keywords": [...] }` - count + list both available.
- Active pool semantics: keywords POSTed since the most-recent composition are "active"; once the composer fires, they reset. The IMMUTABLE journal preserves history forever - operator can audit "what was I curious about last spring?" via SPQL queries.
- **404 / 400 handling**: POST with non-list body → 400. POST with empty list → 400. POST with all-empty entries → 400. The endpoint is permissive: whitespace-only / non-string entries are silently skipped.

**3 new settings:**
- `curator_keyword_boost_enabled` (bool, default True)
- `curator_keyword_boost_amount` (float, default 0.2)
- `curator_keyword_pool_fallback_seconds` (int, default 86400 = 24h)

---

### Slice 12 (`bf3c51c`) - Cross-source search endpoint

**What changed:**
- ONE new REST endpoint: `GET /api/search?q=<query>&sources=<...>&limit=<N>`.
- Searches the already-ingested candidate pool (no real-time yt-dlp). Returns the same JSON shape as `/api/playlist/today` so the renderer reuses one code path.

**What speaktube needs to UPDATE:**
- The new Discover view's "Search now" button can fetch `GET /api/search?q=<query>`. The response is renderable by the existing playlist render path.
- `sources` query param accepts a comma-separated list of source enum values (e.g. `?sources=youtube_rss,archive_org`). Default: all sources. The player can expose a per-source toggle in a future iteration; v1 can just send the default.
- `limit` defaults to 100; max 1000. Invalid `limit` (non-int) falls back to 100.
- Empty `q` returns 400. Empty result returns 200 with `items: []` (same shape as the playlist's "nothing today" case - but for search, "no matches" is a valid 200, not a 404).

**Render notes for search results:**
- `items[].slot_kind` is always `"main"` (no LLM slot classification on search).
- `items[].rationale` is always empty string.
- `video.interest_score = 1.0` (user explicitly asked).
- `video.growth_score = null` (not meaningful for ad-hoc search).
- `video.slop_score` is computed via the same regex as the composer feeder.
- `video.score_reasoning` carries `"Matched search: <q>"` for inline display.
- Results sorted by `_epoch DESC` (most-recently-discovered first).

**No new endpoint configuration** - speaktube can call immediately after deploy. No new settings, no new schema, no PUT-to-runtime step.

---

## Cross-cutting contract guarantees (forward-compat)

These have NOT changed across slices 4-12 and remain stable:

1. **404 on `/api/playlist/today` when no composition exists** (slice 1 contract).
2. **200 + `dignity_pct: null` on `/api/dignity/today` when no plays exist** (slice 1 contract).
3. **All known fields are always present** in the `video` object - empty string is the load-bearing "no value" signal. Renamed / removed fields are breaking; new fields are additive.
4. **`video.position` is 1-indexed unique sequential** (slice 5 onward).
5. **`video.external_id` is unique per composition** (slice 5 onward).
6. **Unknown fields are preserved verbatim** in JSON responses - speaktube can ignore anything it doesn't recognize.

---

## Summary of NEW endpoints

| Endpoint | Method | What it does | Slice |
|---|---|---|---|
| `/api/preferences/keywords` | `POST` | Seed keyword pool for next composer fire | 11 |
| `/api/preferences/keywords` | `GET` | Return active keyword pool | 11 |
| `/api/search` | `GET` | Ad-hoc cross-source search | 12 |

## Summary of NEW response fields (on existing endpoints)

| Endpoint | Field | Type | Slice |
|---|---|---|---|
| `/api/playlist/today` | `video.thumbnail_url` | string (may be `""`) | 4 |
| `/api/playlist/today` | `video.published_at` | string (may be `""`) | 4 |
| `/api/playlist/today` | `growth_dial_stored` | float (`-1..+1`) | 10 |
| `/api/playlist/today` | `thin_history_active` | bool | 10 |

## Summary of NEW IMMUTABLE schemas

| Schema | Purpose | Slice |
|---|---|---|
| `curator_keyword_prefs` | Operator-supplied keyword preferences (forever-data) | 11 |
| `curator_playlist.thin_history_active` (column added) | Was thin-history mode active at compose time | 10 |
| `curator_playlist.thumbnail_url`, `published_at` (columns added) | Item thumbnail + publish date threaded through to playlist | 4 |
| `curator_candidates.thumbnail_url` (column added) | Per-source thumbnail URL | 4 |

---

## Open items / future deliveries

Speaktube's `SPEAKESQUERY_REQUESTS.md` includes a few asks NOT covered in this delivery; they're on the SpeakesQuery roadmap:

| Req | Status | Notes |
|---|---|---|
| #7 - `GET /api/playlist/preview?growth_dial=X` for live slider preview | **Deferred** | Speaktube called this "optional"; the daily-fire cadence covers 95% of the use case. Revisit if the player UX needs it. |
| #8 Tier 2 sources (DailyMotion, Vimeo, Tubi, Pluto TV, Roku Channel) | **Future** | Each is its own slice via the six-place wiring recipe codified in `reference_adding_curator_candidate_source_six_place_wiring.md`. Archive.org (Tier 1, slice 7) is the working template. |
| #8 Tier 3 sources (PeerTube, Odysee, BitChute, Rumble) | **Future, slop-tuning first** | Per the spec, alt-tech sources need slop-scoring tuned per source profile before ingestion is enabled. |
| #8 Tier 4 sources (Library of Congress, Wikimedia Commons) | **Future** | Likely needs bespoke extractor logic (LoC's URL patterns aren't fully yt-dlp-supported). |

---

## Contact / sync points

* **In-repo doc per slice:** every commit in slices 4-12 has its full per-slice rationale in the commit message body (`git log --oneline 6174cd3..bf3c51c` then `git show <hash>` for any line).
* **The canonical SpeakesQuery-side contract reference:** `docs/lang/21_curator_speaktube.md`.
* **The original build guide** (pre-slices-4-12): `docs/lang/22_speaktube_handoff.md` - kept up-to-date inline as slices landed.
* **This doc** (`23_speaktube_post_launch_deltas.md`) is a snapshot covering slices 4-12. Future multi-slice deliveries will produce a new `24_*.md`.

Questions / clarifications: open an issue on the project's public issue tracker.
