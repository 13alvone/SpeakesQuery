# Speaktube Build Handoff

**Date**: 2026-05-17. **SpeakesQuery side**: shipped + verified end-to-end on `the SpeakesQuery host`. **Goal**: speaktube up and running TODAY.

This doc is the build companion to [21_curator_speaktube.md](21_curator_speaktube.md) (the contract spec). Read this to know **what speaktube needs to build**, in what order, against what real data, with what known gotchas.

---

## TL;DR

* SpeakesQuery delivers **4 REST endpoints** + a **canonical playlist JSON shape** + a **telemetry NDJSON ingestion pull** + the **growth-dial setting** round-trip. All live, all tested, all already producing a real playlist composed by Claude Sonnet 4.6 from your real watch history + topic-clustered candidate pool.
* Speaktube delivers: a **player UI** (renders the playlist + collects telemetry), a **small HTTP server** (hosts the player, exposes telemetry NDJSON for SpeakesQuery's hourly pull, proxies SpeakesQuery's `/api/*`), and **persistence** for the user's local growth-dial slider state.
* **Today's MVP**: load `/api/playlist/today`, render 14 cards, capture play/skip/rate events, write them to `<speaktube>/api/telemetry/<YYYY-MM-DD>.jsonl`. Everything else (reflections, dignity-pct rendering, theme display, slot-kind badges) is layer-2 polish.

---

## What SpeakesQuery is delivering (live as of 2026-05-17)

### 1. The composed playlist

**Endpoint**: `GET http://localhost:5111/api/playlist/today` (optionally `?date=YYYY-MM-DD`)

**Shape** (verified live; this is the actual payload):

```json
{
  "run_date": "2026-05-17",
  "growth_dial": -0.7,
  "theme": "Truth Hunting Sunday",
  "items": [
    {
      "position": 1,
      "slot_kind": "main",
      "rationale": "Top affinity pick from a familiar channel.",
      "video": {
        "external_id": "q1MZYQGAEQM",
        "url": "https://www.youtube.com/watch?v=q1MZYQGAEQM",
        "title": "\"Doctor\" Cheyenne Bryant EXPOSED For Fake Degrees",
        "channel_name": "Young Don Reacts",
        "thumbnail_url": "https://i.ytimg.com/vi/q1MZYQGAEQM/hqdefault.jpg",
        "published_at": "2026-05-16T18:30:00+00:00",
        "duration_seconds": null,
        "interest_score": 1.0,
        "growth_score": 0.0,
        "slop_score": 0.1,
        "score_reasoning": "Maximum interest_score and zero growth_score..."
      }
    }
  ]
}
```

**Empty states**:
* `404 NoPlaylistComposed` when no playlist has ever been composed (fresh install).
* `200` with `items[]` non-empty when a composition exists.

**Field semantics**:
* `slot_kind`: `"main"` (high-affinity) / `"surprise"` (exploration) / `"movie"` (long-form).
* `rationale`: 1-sentence user-facing "why this is here".
* `interest_score`: now **topic-cluster cosine similarity** (slice 3a) - NOT raw watch-count. Higher = closer to your topical interest centroids.
* `growth_score`: 0.0 for familiars; 1.0 for never-watched channels.
* `slop_score`: 0.0-1.0 clickbait heuristic.
* `duration_seconds`: nullable - yt-dlp flat search doesn't populate it for slice 3b discoveries.
* `thumbnail_url`: image URL for the card (added slice 4, 2026-05-17). Empty string is a valid value - render the placeholder / fall back to YouTube synthesis when so. Never null; the key is always present in the response.
* `published_at`: ISO 8601 publication date (added slice 4, 2026-05-17). Empty string is valid; treat the same as "not provided" for sort + relative-time rendering.
* `position`: 1-indexed, unique, sequential within `items[]` (slice 5, 2026-05-17). The dispatcher renumbers server-side after dedup so the player can trust the value. The defensive client-side `dedupeByExternalId` + `idx + 1` rank rendering remain harmless but redundant for compositions from 2026-05-17 onwards.
* **No duplicate `external_id`s within a composition** (slice 5, 2026-05-17). The dispatcher's keep-first dedup runs at parse-time. The first occurrence wins (its rationale survives); later duplicates are dropped with a warning log.
* **Hybrid expansion** (slice 6, 2026-05-17): the LLM composes positions 1..~20 with full rationale + slot_kind; the dispatcher then appends positions ~21..500 (default) from the scored-candidate pool. Bulk-fill rows carry empty `rationale` + `slot_kind="main"` + `score_reasoning=""` + scores from the feeder. The default playlist size is **500 items** unless the operator overrides `curator_playlist_target_count` (range 20-5000; 20 disables bulk-fill). For UI purposes: treat `rationale === ""` as "compact card / no disclosure" and `rationale !== ""` as "full card with disclosure". Both share the same JSON shape.
* **Channel cooldown** (slice 9, 2026-05-17 - req #5): no channel exceeds 10% of the playlist (50 items in a 500-item playlist), and no channel has more than 3 items in any 10-position window. Enforced on bulk-fill server-side; the LLM portion is governed by the same rule in the prompt. The speaktube renderer no longer needs to client-side reorder for diversity - the response is already diversified.
* **Thin-history mode** (slice 10, 2026-05-17 - req #12): two new top-level response fields. `growth_dial_stored` returns the operator's slider value as-stored; `growth_dial` returns the EFFECTIVE value the composer used (= stored + thin-history bias when active). `thin_history_active` is a bool - true when the user has watched less than `curator_thin_history_threshold_seconds` in the trailing 30 days, in which case the dispatcher boosted the effective dial by `curator_thin_history_dial_bias` (default +0.5, clamped to +1.0). Speaktube can render the divergence: e.g. "Your slider is at -0.7 - boosted to -0.2 because you've only watched 2 hours in the last 30 days." Both fields are always present in the response; `thin_history_active` defaults to `false` for compositions written pre-slice-10. The dial-injection bug from req #12.3 is also fixed in this slice: previously the LLM saw "defaults to -0.7" hard-coded text regardless of the slider, so the dial had zero effect on composition.
* `theme`: optional 1-3 word vibe tag. The composer occasionally generates evocative themes like "Truth Hunting Sunday" - render it if non-empty, hide if empty.

### 2. The dignity score

**Endpoint**: `GET /api/dignity/today`

**Shape**:

```json
{ "total_plays": 0, "chosen_plays": 0, "dignity_pct": null }
```

**Empty state**: returns 200 with `dignity_pct: null` when no plays yet (NOT 404). speaktube renders this as "offline" - distinguishes "no telemetry today" from "your dignity is 0%".

**Semantics**: `dignity_pct = chosen_plays / total_plays * 100`. A "chosen" play is one whose `chosen_by` is `"curator"`, `"user_manual"`, or `"playlist"` - NOT `"recommendation"`. The whole point of the platform.

### 3. Reflection submission

**Endpoint**: `POST /api/reflections`

**Body**:
```json
{
  "kind": "eod",         // or "per_video"
  "content": "free text reflection content (markdown ok)",
  "date": "2026-05-17",  // optional; defaults to today (operator's local TZ)
  "video_external_id": ""  // required when kind=per_video
}
```

**Response**: `{ "status": "success" }` on 200.

### 4. Growth-dial round-trip

**Endpoint**: `POST /api/growth_dial`

**Body**: `{ "value": -0.4 }` (-1.0..+1.0 bipolar float, slice 8 2026-05-17 - was 0.0..1.0)

**Response**: `{ "status": "success", "value": 0.25 }`

**Read it back**: `GET /api/settings` returns `curator_growth_dial: -0.4` (or whatever the bipolar value is) among the full settings map. Default is `-0.7` (mostly familiar). The player typically just owns local state and POSTs on slider release - read-back is for operator inspection.

### 5. Telemetry pull (SpeakesQuery → speaktube)

SpeakesQuery's hourly `curator_telemetry_pull` ingestion script HTTP-fetches NDJSON from:

```
GET http://<speaktube-host>:8080/api/telemetry/<YYYY-MM-DD>.jsonl
```

This is **speaktube's responsibility to expose** - one event per line, one file per day. Each line is a JSON object with the canonical event shape:

```json
{"event_type":"play_start","event_ts":"2026-05-17T09:14:22-07:00","video_external_id":"q1MZYQGAEQM","chosen_by":"curator","run_date":"2026-05-17","position":1}
{"event_type":"play_end","event_ts":"2026-05-17T09:42:01-07:00","video_external_id":"q1MZYQGAEQM","chosen_by":"curator","watched_seconds":1660,"total_seconds":1742}
{"event_type":"skip","event_ts":"2026-05-17T10:01:00-07:00","video_external_id":"shorts_xyz","chosen_by":"recommendation","watched_seconds":8,"total_seconds":42}
{"event_type":"rate","event_ts":"2026-05-17T10:30:00-07:00","video_external_id":"q1MZYQGAEQM","chosen_by":"curator","rating":8}
{"event_type":"mark_junk","event_ts":"2026-05-17T10:35:00-07:00","video_external_id":"junk_clip","chosen_by":"recommendation","reason":"clickbait thumbnail"}
{"event_type":"reflection_submit","event_ts":"2026-05-17T22:00:00-07:00","kind":"eod","content":"Today felt deep and intentional."}
{"event_type":"manual_search","event_ts":"2026-05-17T14:00:00-07:00","query":"how rare earth magnets work"}
{"event_type":"impression","event_ts":"2026-05-17T09:14:00-07:00","video_external_id":"q1MZYQGAEQM","chosen_by":"curator"}
```

**Required fields**: `event_type`, `event_ts` (ISO 8601 WITH timezone offset).

**Event-type-specific fields**:
* `play_start` / `play_end` / `skip` / `rate` / `mark_junk` / `impression`: `video_external_id`, `chosen_by`.
* `play_end` / `skip`: + `watched_seconds`, `total_seconds`.
* `rate`: + `rating` (integer 1–9).
* `mark_junk`: + `reason` (free text).
* `reflection_submit`: + `kind`, `content` (also flows through SpeakesQuery's own POST /api/reflections - pick one path, don't double-write).
* `manual_search`: + `query`.

**`chosen_by` enum**: `"curator"` (from `/api/playlist/today`) / `"user_manual"` (user typed in search) / `"recommendation"` (suggested by player after curator playlist exhausted) / `"playlist"` (continuation of a multi-video playlist). The dignity score depends on this.

**File rotation**: one file per local date. Speaktube can append-on-disk; SpeakesQuery's pull is idempotent (dedup by `event_ts + event_type + video_external_id`).

**Empty state**: speaktube MUST return 404 for dates that don't exist (NOT empty JSON, NOT 200 with empty body). SpeakesQuery's puller treats 404 as "no events that day, skip."

---

## What speaktube must build

### Layer 1 - Day-1 MVP (target: working today)

Components:

1. **Player UI** - a static page (HTML + JS) that:
   * Fetches `GET /api/playlist/today` on load.
   * Renders 14 cards in order. Each card shows: title, channel_name, rationale, slot_kind badge ("main" / "surprise" / "movie"), and a "Why this is here" expandable disclosure for score_reasoning.
   * Has a Play button per card that opens the YouTube video (an embedded `<iframe>` is fine for v1; full custom player can come later).
   * Tracks play_start when video starts, play_end when video ends (or skip when user advances early).
   * Captures rate (1-9 buttons or slider) and mark_junk (button with optional text reason).
   * POSTs every event as a JSON line to `POST /api/telemetry/event` on the speaktube backend.

2. **Speaktube backend** - a small HTTP server (Node/Python/Go, your call) that:
   * Serves the player static page.
   * Exposes `POST /api/telemetry/event` - appends the body line to `<data>/<YYYY-MM-DD>.jsonl` (local timezone date).
   * Exposes `GET /api/telemetry/<YYYY-MM-DD>.jsonl` - returns the file as `text/plain` (or `application/x-ndjson`), or 404 if the file doesn't exist. **This is what SpeakesQuery's hourly pull reads.**
   * Reverse-proxies `GET /api/playlist/today` and `GET /api/dignity/today` → `http://localhost:5111/api/...` so the player code never CORS-handshakes with SpeakesQuery directly.

3. **Configuration** - speaktube needs to know:
   * SpeakesQuery base URL (default `http://localhost:5111`).
   * Local telemetry data directory (default `./data/telemetry/`).
   * Listen port (default `8080`).

That's enough for the curator → player loop to close end-to-end TODAY.

### Layer 2 - Polish (target: this week)

Once Layer 1 is rendering real videos:

4. **Growth-dial slider** - a -1.0..+1.0 BIPOLAR slider in the player UI (slice 8, 2026-05-17 - was 0.0..1.0). On release: `POST /api/growth_dial { "value": <new> }` (proxy → SpeakesQuery). On page load: `GET /api/settings` (proxy) and initialize the slider from `curator_growth_dial` (default -0.7). Negative = familiarity bias; positive = exploration bias; 0.0 = balanced.

5. **Dignity-pct display** - fetch `GET /api/dignity/today` on a 60s timer. Render `dignity_pct` as a footer badge - green if >= 75, amber 50-74, red < 50. When `dignity_pct: null`, render as "offline" or hide.

6. **Reflection input** - a "How did today feel?" textarea in the footer. On submit: POST to both speaktube's telemetry (with `event_type: "reflection_submit"`) AND SpeakesQuery's `/api/reflections` directly. Single-write is fine; pick whichever path you prefer.

7. **Theme display** - if `theme` is non-empty in the playlist response, render it as a subtle header tag ("Today's theme: Truth Hunting Sunday").

8. **Manual search** - a search bar that POSTs `manual_search` telemetry events on submit + opens a YouTube search results page (the rendered videos can be played via the same play_start/play_end flow with `chosen_by: "user_manual"`).

### Layer 3 - Forever-iteration (target: ongoing)

9. **Curated playback player** - replace the YouTube iframe with a yt-dlp-backed custom player. This unlocks: skip-ad enforcement, lossless quality control, ability to play non-YouTube sources (PeerTube, Archive.org, etc.) as slice 3c+ adds them. Heavier lift; not required for the loop to close.

10. **Time-of-day mode** - different visual themes / playlist filtering based on local time (morning calm, afternoon focus, evening wind-down).

11. **Post-watch reflection prompts** - after a play_end, optionally prompt for a per-video reflection ("anything you took from this one?").

---

## Today's hour-by-hour plan

```
Hour 1 - Spin up speaktube repo, write the backend skeleton with three routes:
         - GET  /
         - POST /api/telemetry/event
         - GET  /api/telemetry/<YYYY-MM-DD>.jsonl
         Verify: curl POST a synthetic event, then curl GET the date file
         and see the line.

Hour 2 - Wire the reverse proxy:
         - GET  /api/playlist/today  →  http://localhost:5111/api/playlist/today
         - GET  /api/dignity/today   →  http://localhost:5111/api/dignity/today
         - POST /api/reflections     →  speakesquery
         - POST /api/growth_dial     →  speakesquery
         Verify: curl your speaktube and see SpeakesQuery's response.

Hour 3 - Build the player static page (vanilla HTML+JS is fine):
         - fetch /api/playlist/today
         - render the items as cards
         - Play button opens a YouTube iframe per card
         - Skip / Next buttons advance the queue
         - Track play_start / play_end via the iframe's API

Hour 4 - Wire telemetry POST on play_start, play_end, skip events.
         Confirm: refresh the speaktube /api/telemetry/<today>.jsonl
         in a browser tab; events appear after each playback action.

Hour 5 - Smoke-test the full loop:
         1. Open speaktube in browser → see real playlist (14 items).
         2. Click Play on a few items → events accumulate.
         3. Wait 60 min OR manually trigger SpeakesQuery's curator_telemetry_pull
            via /api/si/<task_id>/run.
         4. Query SpeakesQuery: 
              GET /api/dignity/today
            should now return a real dignity_pct (non-null) reflecting
            today's events.

Hour 6 - Ship rate / mark_junk UI controls + persistence sanity.
         Done - speaktube end-to-end working with real curator data.
```

After hour 6 you have a working anti-algorithm player rendering the curator's actual composed playlist.

---

## Known gotchas (read before you build)

These are extracted from the SpeakesQuery-side memory; each one cost the SpeakesQuery side time today. Pay them forward:

* **`/api/playlist/today` returns 404 - not 200 + empty items[] - when no playlist exists.** Render the 404 as "Curator is not wired up yet" or similar. Speaktube must distinguish "no curator" (404) from "you literally have a quiet day" (200 + 0 items - won't happen today but might in future).

* **`/api/dignity/today` returns 200 + `dignity_pct: null` (NOT 404) when zero telemetry.** Always 200; null is the empty-state sentinel. Render as "offline."

* **Empty telemetry file date → 404, NOT 200 with empty body.** SpeakesQuery's hourly puller treats 404 as "skip"; an empty 200 would be processed as a failed parse.

* **Telemetry `event_ts` MUST include timezone offset.** SpeakesQuery normalizes to UTC internally but needs the offset to do that. Use ISO 8601 like `2026-05-17T09:14:22-07:00`, not naive `2026-05-17T09:14:22`. A naive timestamp is a 7-hour lie for a PT user.

* **`chosen_by` is load-bearing for the dignity score.** Default it to `"recommendation"` for any video the user opens that didn't come from the curator's playlist - that's the honest accounting. Curator picks default to `"curator"`. Manual search results default to `"user_manual"`.

* **Don't trust YouTube's iframe play events 1:1.** YouTube fires `play` for autoplay, ad-start, ad-end, etc. Throttle / debounce so one play_start per intentional user start. For v1, just record on the first `play` event per video; refine later.

* **Slot-kind `"surprise"` is the breadth piece.** Visually badge these differently - they're explicitly channels the user has never watched. Render them with extra context ("New to your rotation") so the user doesn't feel like the curator suddenly hallucinated channels.

* **Don't cache the playlist client-side past the day boundary.** A composition is keyed by `run_date`; midnight local-time should trigger a re-fetch.

* **The growth_dial is a USER setting, not a per-fetch param.** Once the user sets it (e.g. `-0.4`), every future composer fire uses that value. Don't pass it on the playlist GET - just POST when the user moves the slider. Bipolar range `-1.0..+1.0` (slice 8 2026-05-17); negative = familiarity, positive = exploration.

* **TLS / hostname**: SpeakesQuery and speaktube can run on the same machine (`localhost`) or on separate LAN hosts. If you use LAN hostnames your DNS doesn't resolve, add them to `/etc/hosts`. SpeakesQuery is HTTP-only by design (no TLS termination); same for speaktube. This is fine on a trusted LAN.

---

## Sanity-check commands (run these from any LAN host)

```sh
# Verify SpeakesQuery is up and producing a real composition
curl -s http://localhost:5111/api/playlist/today | python3 -m json.tool | head -40
# Expected: run_date 2026-05-17, theme "Truth Hunting Sunday", 14 items,
#           channels include House of Highlights, Chilling Scares, WION,
#           NostalgiaEthereal, MeidasTouch, Aaron Parnas, etc.

# Verify the dignity endpoint
curl -s http://localhost:5111/api/dignity/today
# Expected: {"total_plays":0,"chosen_plays":0,"dignity_pct":null}
# (Once speaktube starts sending events, these numbers will populate.)

# Probe SpeakesQuery's settings (read the growth_dial)
curl -s http://localhost:5111/api/settings | python3 -c "
import sys, json; print(json.load(sys.stdin)['settings']['curator_growth_dial'])
"
# Expected: 0.15
```

Once your speaktube backend is up:

```sh
# Synthetic event submission
curl -sX POST http://localhost:8080/api/telemetry/event \
  -H 'content-type: application/json' \
  -d '{"event_type":"play_start","event_ts":"2026-05-17T09:14:22-07:00","video_external_id":"q1MZYQGAEQM","chosen_by":"curator","run_date":"2026-05-17","position":1}'

# Read the day file back
curl -s 'http://localhost:8080/api/telemetry/2026-05-17.jsonl'
# Expected: the JSON line you just POSTed.

# Verify SpeakesQuery can pull it (trigger the hourly puller manually)
# (assumes the curator_telemetry_pull ingestion task ID is N; check /api/si/list)
curl -sX POST http://localhost:5111/api/si/N/run

# Then confirm SpeakesQuery saw the event
curl -s 'http://localhost:5111/api/dignity/today'
# Expected: total_plays 1, chosen_plays 1, dignity_pct 100.0
```

When that round-trip works, speaktube is integrated.

---

## What SpeakesQuery is NOT delivering (you'll build elsewhere)

* **Authentication** - both sides are LAN-trust. If speaktube ever escapes the LAN, add auth at speaktube's edge.
* **Multi-user state** - the curator targets ONE specific user. If you want family-shared speaktube, partition state by user-id at speaktube's level.
* **Catch-up playlists** - there's no `/api/playlist/yesterday`. If you want to render past playlists, use `/api/playlist/today?date=YYYY-MM-DD`.
* **The actual YouTube video stream** - that comes from YouTube itself (for v1) or yt-dlp (for v3 custom player). SpeakesQuery just provides the URL.
* **The growth-dial slider rendering** - speaktube owns the UI; SpeakesQuery just provides the round-trip POST endpoint.

---

## When speaktube is "done enough"

A "ship it Friday" definition of done:

* `GET /api/playlist/today` rendered as 14 playable cards in the player UI.
* User can play / pause / skip videos; events fire telemetry.
* `GET /api/telemetry/<today>.jsonl` returns the accumulated events.
* SpeakesQuery's hourly pull reads them; `GET /api/dignity/today` returns a real `dignity_pct`.
* Reflection textarea POSTs to `/api/reflections` and the reflection lands in SpeakesQuery's IMMUTABLE log (verify: `SPQL index="indexes/IMMUTABLE/curator_reflections/*" | head 5`).

After that, every Layer 2 + 3 enhancement is iteration without integration risk.

---

## Where SpeakesQuery's docs live

* [21_curator_speaktube.md](21_curator_speaktube.md) - the formal contract spec (endpoints, schemas, deploy steps).
* [22_speaktube_handoff.md](22_speaktube_handoff.md) - this file (build companion).
* [16_immutable_data_namespace.md](16_immutable_data_namespace.md) - why `indexes/IMMUTABLE/*` matters for the trade-off between forever-data and cleanup-eligible-data.
* [03_functions.md](03_functions.md) + [02_commands.md](02_commands.md) - SPQL reference if you want to query the telemetry / playlist parquet logs directly for operator inspection.

The full SpeakesQuery side is on `origin/main` in commits `33a090e → 626abe8`. The curator slice 3a + 3b code is documented in detail at [21_curator_speaktube.md](21_curator_speaktube.md).

---

## Cost note

The composer currently fires once a day at 05:00 PT (`0 5 * * *` in `America/Los_Angeles`). Cost per fire: **~$0.15** with the current pool (180 candidates × ~16500 input tokens × Claude Sonnet 4.6 rate of $3/M). Daily cost: $0.15. Monthly: ~$4.50. Annual: ~$55. Well within the "compounding personal infrastructure" budget.

The composer ships with `dry_run: true` as the safe default - flip it OFF (via PUT `/api/alert-groups/curator_playlist_composer` with `{"dry_run": false}`) when you're ready for tomorrow's 05:00 PT cron to produce a real composition. Until then, the dry-run preview path emits a placeholder row without calling Claude.

---

**Hand-off is complete.** The contracts are stable, the data flows, the cost is bounded. Speaktube can render against `http://localhost:5111` starting now.
