# Failed-feeder Patch Drafter

> Phase 4 / Bet 4 slice 8a. When an ingestion script fails, ask Claude
> to suggest a unified-diff fix. Recorded to a Parquet log for
> operator review - never auto-applied. Slice 8b will add GitHub PR
> creation on top.

## What it does

Every scheduled ingestion script can fail - upstream API outages,
schema changes, missing credentials, sandbox-disallowed imports,
malformed responses. When one does, the engine logs the failure as
usual (`indexes/logs/ingestion/*`). With the **patch drafter**
enabled, the engine ALSO asks Claude to read the script source +
the error message and suggest a unified diff that should fix the
issue.

The suggestion lands in a new Parquet log:
`indexes/logs/patch_suggestions/*`. The operator queries it with
SPQL like any other log:

```spql
index="indexes/logs/patch_suggestions/*"
| sort -_epoch
| head 20
```

To review a specific suggestion:

```spql
index="indexes/logs/patch_suggestions/*"
| where task_id="42"
| sort -_epoch
| head 1
| table _epoch, status, model, cost_usd, error_message, patch, explanation
```

Slice 8b-1 (2026-05-09) added an **inline review surface** on the
Ingestions page - see "Inline review on the Ingestions page" below.

The diff is opaque text - the operator reviews + applies manually.
**The drafter never auto-applies a patch and never opens a PR
(slice 8b-2 deliverable; may slide to Phase 6).**

## Inline review on the Ingestions page (slice 8b-1)

Each row in the Ingestions page whose last run failed gets a
secondary row beneath it containing a **"💡 Recent fix suggestion"**
disclosure. Click to expand → the SPA fires the SPQL query above
against the existing `/api/query` endpoint, and renders the most
recent suggestion inline:

* **Status pill** - colored by `status` field. `success` is green;
  `dry_run` / `skipped_budget` / `skipped_no_key` are amber; `error`
  is red.
* **Metadata row** - model, cost, latency, ISO timestamp,
  request_id (joins to `claude_api_history.sqlite`).
* **Suggested diff** - rendered in a monospace `<pre>` block with
  light coloring: green for `+` (additions), red for `-`
  (deletions), italic gray for `@@` / `---` / `+++` / `diff` /
  `index` header lines.
* **Explanation** - Claude's plain-English reasoning, rendered in
  a styled `<blockquote>`-like panel.

The disclosure is collapsed by default - the lazy-load fires only on
the FIRST expand. Subsequent collapse / re-expand cycles use the
cached result (no redundant `/api/query` round-trip).

**No backend changes for slice 8b-1.** Per the slice-7 principle
(`reference_reuse_existing_endpoint_for_ui_surface.md`): a new UI
surface for an existing capability does NOT justify a new endpoint
when the existing one (here, `/api/query`) already serves it
unchanged.

**Defense-in-depth.** The SPQL query interpolates `task_id` into a
`where` clause string-literal. The JS escapes embedded double
quotes before building the query so a malformed id can't break out
of the literal. Task ids are server-issued integers in normal
operation; the escape is a belt-and-suspenders safeguard.

The **secondary row carries `data-si-suggestion-for="<task_id>"`,
NOT `data-si-task-id`**, so the existing Pipeline Check cross-tab
nav (`tr[data-si-task-id="X"]`) keeps unambiguously targeting the
primary row. This is pinned by
`tests/test_patch_drafter_ui_slice8b1.py::TestDataAttributeBoundary`
per the CLAUDE.md "Do Not" entry on the `data-si-task-id` contract.

### What the disclosure shows when there's no suggestion yet

* **Drafter disabled (default).** "No suggestion available for this
  task. The patch drafter is OFF by default - enable it in Settings
  → Failed-feeder Patch Drafter to get auto-suggested fixes for
  future failures."
* **Drafter enabled but no failures yet.** Same message - the
  next terminal failure will produce a suggestion.
* **Drafter enabled, failure happened, but `skipped_budget`.**
  The pill is amber and reads `skipped_budget`; the disclosure
  body explains the worst-case estimate exceeded `patch_drafter_max_cost_usd`
  and recommends raising it.

## Enabling

The drafter is **OFF by default** - opt-in. Enable via
**Settings → Failed-feeder Patch Drafter** in the SPA, or by
setting in `global_settings.yaml`:

```yaml
patch_drafter_enabled: true
```

You can also tune:

| Setting | Default | Range | Notes |
|---------|---------|-------|-------|
| `patch_drafter_enabled` | `false` | bool | Master switch. |
| `patch_drafter_model` | `claude-haiku-4-5-20251001` | model id | Switch to Sonnet for higher-quality diffs. |
| `patch_drafter_max_cost_usd` | `0.10` | 0–1000 USD | Per-call hard ceiling. `0.0` = uncapped (NOT recommended). |
| `patch_drafter_timeout_seconds` | `60` | 5–600 s | Per-call timeout. |

## How it works

### Trigger

The drafter fires on any **terminal** ingestion task failure -
i.e. after all retry attempts have been exhausted. Both the non-
retryable path (`ValueError`/`SyntaxError` - bad code, bad config)
and the retryable path's final-attempt branch dispatch the drafter.
Mid-retry failures don't trigger; they may resolve themselves on
the next attempt.

### Dedup by error hash

A script that fails the SAME way every cron tick produces ONE
suggestion, not N. The engine maintains a per-task `error_hash` ↔
last-suggested mapping; identical hash on a subsequent failure
short-circuits the dispatch.

The `error_hash` is `sha256(error_message)` truncated to 16 hex
characters - stable across runs, content-addressed. Restarting the
engine resets the cache (intentional: gives the operator a chance
to see the suggestion again in case they missed it).

### Background dispatch

The drafter runs on a daemon thread spawned per dispatch - the
APScheduler worker is freed immediately. Errors inside the
dispatch (Claude outage, missing API key, log emit failure) NEVER
bubble back to the engine. The drafter is value-add; it must not
destabilise the ingestion pipeline.

### Budget gate (slice-7 contract)

The drafter honors the slice-7 LLM-pipe budget gate:

* A **conservative-by-design** worst-case cost estimate is computed
  before any network call. Estimate = (input tokens at full
  prompt) × input pricing + (max output tokens) × output pricing.
* If the estimate exceeds `patch_drafter_max_cost_usd`, the call
  is **skipped** with `status="skipped_budget"` - no network call,
  no cost. The skip is logged so the operator can SPQL-query and
  raise the cap if appropriate.
* `dry_run=true` (programmatic; no setting) returns the estimate
  without calling. Used by the **money-leak canary** test that
  pins zero `call_messages_create` invocations across all
  bypass paths.

### Cost transparency

Every Claude call from the drafter routes through
`analyzers.claude_client.call_messages_create()` - the canonical
wrapper. Cost shows up in two places:

* `claude_api_history.sqlite` - the per-call SQLite history
  (gzipped request + response, latency, tokens, retry count).
  Joins to a patch suggestion via `request_id`.
* `indexes/logs/patch_suggestions/*` - the Parquet log row
  (immediate operator-facing).

Daily-budget accounting (`claude_analyzer_daily_budget_cents`)
applies - drafter calls count against the same ceiling as alert-
group dispatches and the analyzer.

## Schema

`patch_suggestions` log columns:

| Column | Description |
|--------|-------------|
| `_epoch` | Suggestion event Unix seconds |
| `task_id` | Ingestion task id (or `""` for ad-hoc) |
| `title` | Ingestion script title |
| `error_hash` | 16-hex stable hash of the error_message (dedup key) |
| `status` | `success` / `dry_run` / `skipped_budget` / `skipped_no_key` / `error` |
| `model` | Claude model id used |
| `cost_usd` | Actual cost (success) or worst-case estimate (dry_run) |
| `latency_ms` | Network call duration |
| `patch` | Unified diff text (may be empty if `NO_CONFIDENT_FIX`) |
| `explanation` | Plain-English explanation |
| `request_id` | Joins to `claude_api_history.sqlite` |
| `error_message` | Original ingestion error (truncated to 1000 chars) |
| `input_tokens` | Actual / worst-case input tokens |
| `output_tokens` | Actual / worst-case output tokens |
| `drafter_error_class` | Populated only on `status=error|skipped_*` |
| `drafter_error_message` | Populated only on `status=error|skipped_*` |

The schema is **additive-only** going forward - never remove a
column once shipped. The operator's audit history of suggested
fixes survives indefinitely.

## What Claude is asked

The system prompt frames Claude as a SpeakesQuery-aware code reviewer:

> You are a software engineer reviewing a SpeakesQuery ingestion
> script that failed. SpeakesQuery scripts are Python that runs in a
> RestrictedPython sandbox by default; allowed modules are pandas,
> requests, json, datetime, time, re, math, hashlib, base64,
> collections, io, bs4, lxml. Scripts must produce a DataFrame with
> an `_epoch` column (Unix seconds) and call `GENERATE_RESULTS(df)`
> to emit output. Common failure causes: missing API credentials,
> rate limits, schema changes upstream, missing _epoch column,
> sandbox-disallowed imports, network failures, malformed responses.
>
> Given the script source and the error message, produce a unified
> diff (`diff --git a/script.py b/script.py` style) that you believe
> will fix the failure. Wrap the diff in a ```diff fenced block.
> Then give a one-paragraph plain-English explanation of WHY the
> change should fix the issue.
>
> If you cannot suggest a confident fix from the information given
> (e.g. the error suggests an external service outage that the
> script cannot work around), say so explicitly in plain English
> instead of guessing - output the literal string `NO_CONFIDENT_FIX`
> followed by your reasoning. Do NOT emit a speculative diff just to
> fill the response.

The user message wraps the script source + error in a `<task>`
block. The drafter parses the response into:

* `patch` - content of the first ```diff (or ```patch) fenced block
* `explanation` - everything after the closing fence

Claude responses without a fenced block (the `NO_CONFIDENT_FIX`
case) yield empty `patch` + the full text in `explanation`.

## Security

* **Hardcoded prompt.** The system prompt lives in the module -
  not operator-editable. Operator-editable prompts open a code-
  execution-via-prompt-injection attack surface that's not
  justified for an internal-use diff suggester.
* **Credential redaction.** The error message reaching the
  drafter has already been scrubbed by
  `_redact.redact_credentials` in the engine. API keys never reach
  Claude.
* **No automatic application.** The drafter writes to a log;
  the operator reviews + applies the diff manually. Slice 8b will
  add GitHub PR creation, with the same review-then-merge
  separation.

## Known limitations

* The drafter sees only the FAILING script + error. It doesn't see
  recent successful runs, upstream schema, or git history. Slice 8b
  may add some of this context.
* Long scripts hit the 2048-token output ceiling on Claude's
  response. Patches for genuinely large refactors will get
  truncated; the operator still gets the explanation.
* Budget-gate skip does NOT retry with a different model. If
  Sonnet would fit the budget for a complex script, the operator
  has to switch `patch_drafter_model` manually.

## What's deferred to slice 8b-2 / 9

* GitHub PR creation (slice 8b-2 - may slide to Phase 6 if the auth
  foundation isn't in place; see the slice 8b-2 design discussion).
* AG dispatcher failure-email integration (the drafter currently
  logs to Parquet + the SPA Ingestions page; the AG email could
  include a link to the most recent suggestion).
* Multi-attempt drafting (try Haiku first, escalate to Sonnet if
  Haiku says `NO_CONFIDENT_FIX`).

## See also

* [`09_ingestion_etiquette.md`](09_ingestion_etiquette.md) -
  ingestion script schema + failure modes (the drafter targets these)
* [`11_claude_analyzer.md`](11_claude_analyzer.md) - the canonical
  Claude wrapper this module reuses
* [`14_logging.md`](14_logging.md) - Parquet log structure +
  `cleanup_logs` budget
* [`18_llm_pipes.md`](18_llm_pipes.md) - slice-7 budget-gate
  contract this module mirrors
