# Claude Analyzer - AI-Powered Analysis for Scheduled Searches

The Claude Analyzer is an optional post-processing layer that routes scheduled search results to Claude (Anthropic's AI) for structured interpretation before alerting. When a saved search returns results, the analyzer can provide a prioritised summary, identify actionable items, detect patterns, and optionally filter whether an alert should be sent at all.

> **Key principle:** The analyzer is fully optional. If disabled or if no API key is configured, the pipeline behaves exactly as it does today. It never modifies the raw query results - it adds interpretation alongside them.

---

## How It Works

The analysis step fires after a scheduled search produces results and before alerting. The flow is:

1. Scheduled search executes on its cron schedule
2. Query returns a DataFrame of results
3. **If** the analyzer is enabled globally **and** the saved search has an analyzer prompt assigned:
   - The prompt's `$token$` placeholders are resolved against the result data
   - The resolved prompt (summary-level briefing) and full result JSON are sent to Claude
   - Claude returns a structured JSON analysis (priority, summary, actionable markets, patterns)
4. **If** the filter gate is enabled on the saved search:
   - A yes/no boolean question is evaluated against the analysis
   - If the answer is NO, the email alert is suppressed (results are still saved to parquet)
5. Results are saved to parquet and telemetry is recorded - this always happens regardless of analysis or filter outcome
6. If alerting is not suppressed, the email is sent (optionally enriched with the analysis summary)

---

## Setup

### 1. Install the SDK

```bash
pip install anthropic
```

This is the only new dependency. If the SDK is not installed and the analyzer is disabled, SpeakesQuery starts normally - the import is lazy.

### 2. Get an Anthropic API key

You need an API key from Anthropic to use the Claude Analyzer. Here's how to get one:

1. Go to [console.anthropic.com](https://console.anthropic.com/) and create an account (or sign in).
2. Navigate to **Settings → API Keys** (or visit [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) directly).
3. Click **Create Key**, give it a name (e.g. "SpeakesQuery"), and copy the key. It starts with `sk-ant-`.
4. **Important:** Copy the key immediately - Anthropic only shows it once. If you lose it, you'll need to create a new one.

Anthropic offers a free tier with limited usage. For production workloads, add a payment method under **Settings → Billing**. See the [Cost Controls](#cost-controls) section below for how SpeakesQuery keeps spending predictable.

### 3. Set your API key

On first launch, SpeakesQuery will prompt you to enter your API key. You can also configure it at any time:

On the **Settings** page, scroll to the **Claude Analyzer** section and paste your Anthropic API key into the **API Key** field, then click **Save Key**. The key is encrypted at rest using the same Fernet credential vault that protects ingestion script credentials - it is never stored in config files or environment variables.

Or via the API:

```bash
curl -X POST http://localhost:5111/api/settings/analyzer-key \
  -H "Content-Type: application/json" \
  -d '{"value": "sk-ant-..."}'
```

To verify a key is stored (never returns the key itself):

```bash
curl http://localhost:5111/api/settings/analyzer-key
# {"status": "success", "has_key": true}
```

To remove the stored key:

```bash
curl -X DELETE http://localhost:5111/api/settings/analyzer-key
```

### Testing connectivity

Next to the **Save Key** and **Remove** buttons there is a **Test Claude** button. It fires a 16-token probe against Claude Haiku - the cheapest thing you can do that still exercises auth, network, and the SDK - and reports latency, tokens, cost, and a specific `error_class` when the call fails. If you just typed a key but haven't saved it yet, the button sends the typed value to the test endpoint so you can verify it before committing to the vault.

Or via the API:

```bash
# Test a not-yet-saved key
curl -X POST http://localhost:5111/api/analyzer/test \
  -H "Content-Type: application/json" \
  -d '{"value": "sk-ant-..."}'

# Or test the key currently in the vault
curl -X POST http://localhost:5111/api/analyzer/test -d '{}'
```

A successful response looks like:

```json
{"status": "success", "ok": true, "model": "claude-haiku-4-5-20251001",
 "latency_ms": 420, "input_tokens": 7, "output_tokens": 2,
 "cost_usd": 0.000047, "attempts": 1}
```

A failure response includes `error_class` (`AuthenticationError`,
`APIConnectionError`, `RateLimitError`, ...) so you can tell credentials
apart from network glitches.

Every test call is also recorded in the [history store](#history-store) and the `indexes/logs/claude_api/` Parquet log - treat test probes like any other billable call.

### 4. Enable the analyzer

On the **Settings** page, toggle **Enable Claude Analyzer** to `true` and click **Save**.

Or via the API:

```bash
curl -X PUT http://localhost:5111/api/settings \
  -H "Content-Type: application/json" \
  -d '{"claude_analyzer_enabled": true}'
```

---

## Analyzer Prompts

Analyzer prompts are reusable templates that tell Claude what to look for in the results. You create them once and assign them to saved searches. Each prompt is a YAML file in the `analyzer_prompts/` directory.

### Creating a prompt

Navigate to the **Analyzer Prompts** screen in the application, or use the API:

```bash
curl -X POST http://localhost:5111/api/analyzer-prompts/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "polymarket_volume_spike",
    "description": "Analyzes Polymarket volume spikes for actionable signals",
    "prompt_text": "You are analyzing results from the scheduled search \"$scheduled_search_name$\".\nThis search monitors Polymarket prediction markets and has returned $result_count$ rows.\n\nThe markets showing activity are: $question$\nVolume spike multiples: $spike_multiple$\n\nAnalyze these results and respond with a JSON object containing:\n- alert_priority: CRITICAL, HIGH, MODERATE, LOW, or SKIP\n- summary: one sentence\n- actionable_markets: list of up to 5 objects with question, position (YES/NO), confidence (0-1), reasoning, estimated_roi\n- pattern_detected: string describing any cross-market pattern\n- cross_reference_needed: list of external sources to check"
  }'
```

### Prompt YAML structure

```yaml
name: polymarket_volume_spike
description: Analyzes Polymarket volume spikes for actionable signals
prompt_text: |
  You are analyzing results from the scheduled search "$scheduled_search_name$".
  This search monitors Polymarket prediction markets and has returned $result_count$ rows.

  The markets showing activity are: $question$
  Volume spike multiples: $spike_multiple$

  Analyze these results and respond with a JSON object containing:
  - alert_priority: CRITICAL, HIGH, MODERATE, LOW, or SKIP
  - summary: one sentence
  - actionable_markets: list of up to 5 objects
  - pattern_detected: string
  - cross_reference_needed: list of strings
created_at: '2026-04-07T12:00:00'
updated_at: '2026-04-07T12:00:00'
```

### Managing prompts

| Action | API Endpoint | Method |
|--------|-------------|--------|
| List all | `/api/analyzer-prompts/list` | GET |
| Create | `/api/analyzer-prompts/create` | POST |
| Get one | `/api/analyzer-prompts/<name>` | GET |
| Update | `/api/analyzer-prompts/<name>` | PUT |
| Delete | `/api/analyzer-prompts/<name>` | DELETE |
| View YAML | `/api/analyzer-prompts/<name>/yaml` | GET |
| Validate tokens | `/api/analyzer-prompts/validate-tokens` | POST |

---

## Token Placeholders

Analyzer prompts use the same `$token$` syntax as email body templates. There are two kinds of tokens:

### Global tokens

Global tokens resolve from the saved search metadata and execution context. They are always available regardless of query output.

| Token | Resolves to |
|-------|------------|
| `$scheduled_search_name$` | Name of the saved search |
| `$scheduled_search_description$` | Description field |
| `$scheduled_search_query$` | The SPQL query |
| `$scheduled_search_cron$` | Cron schedule expression |
| `$scheduled_search_lookback$` | Lookback period |
| `$scheduled_search_trigger$` | Trigger type (`once` or `per result`) |
| `$scheduled_search_email$` | Recipient email address |
| `$scheduled_search_created_at$` | When the search was created |
| `$execution_time$` | ISO timestamp of when the query fired |
| `$result_count$` | Total number of rows returned |
| `$column_names$` | Comma-separated list of column names |

### Column tokens

Any `$token$` that matches a column name in the query results resolves to the **distinct values** in that column across all rows.

**Important:** Unlike email body templates (which substitute per-row), analyzer prompt tokens aggregate across the entire result set. If a column has many distinct values, they are truncated:

```
"Arsenal EPL", "Iran NPT", "Scheffler Masters", ... [+] 47 TRUNCATED
```

The truncation limit is controlled by the saved search's `mv_truncate_limit` setting (default 5 for analyzer prompts). This keeps the prompt concise while the full dataset is attached as JSON for Claude to analyze in depth.

### What Claude receives

Each API call sends two pieces:

1. **System message** - the optional boilerplate system prompt (if configured in Settings) followed by the resolved per-search prompt (tokens filled, multi-values truncated). This is the summary-level briefing.
2. **User message** - the full result set as JSON (`result_df.to_json(orient="records")`). JSON is more token-efficient than CSV because column names appear once per record and numeric values don't need quoting.

Claude sees both the concise overview and the full detail, allowing deep analysis while the prompt provides directional context.

### Boilerplate system prompt

The **Boilerplate System Prompt** (configured in Settings → Claude Analyzer) is optional global text that is prepended to every analysis call. Use it for persona framing, output format preferences, or domain-specific instructions that apply to all your scheduled searches.

Example:

```
You are a quantitative analyst specialising in prediction markets.
Always include confidence intervals in your assessments.
Respond with valid JSON only - no markdown, no commentary.
```

If left blank, only the per-search analyzer prompt is used.

### Validating tokens

Use the token validation endpoint to check that all `$token$` placeholders in a prompt will resolve against a query's output:

```bash
curl -X POST http://localhost:5111/api/analyzer-prompts/validate-tokens \
  -H "Content-Type: application/json" \
  -d '{
    "prompt_text": "$scheduled_search_name$ found $question$ with $nonexistent$",
    "query": "index=\"polymarket\" | head 5"
  }'
```

**Response:**

```json
{
  "status": "success",
  "valid": false,
  "global_tokens": ["scheduled_search_name"],
  "column_tokens": ["question"],
  "unresolved": ["nonexistent"],
  "all_tokens": ["nonexistent", "question", "scheduled_search_name"]
}
```

Unresolved tokens are left as literal `$token$` text in the prompt - they don't cause errors, but Claude will see the raw placeholder.

---

## Assigning a Prompt to a Saved Search

Add the `analyzer_prompt` field to your saved search, referencing the prompt name:

```yaml
name: volume_spikes
query: |
  index="polymarket/markets.parquet" earliest="-6h"
  | where spike_multiple > 2
cron_schedule: '0 */6 * * *'
lookback: -6h
email_address: alerts@example.com
analyzer_prompt: polymarket_volume_spike
```

Or via the API:

```bash
curl -X PUT http://localhost:5111/api/ss/volume_spikes \
  -H "Content-Type: application/json" \
  -d '{"analyzer_prompt": "polymarket_volume_spike"}'
```

If `analyzer_prompt` is empty or not set, the search runs without analysis - exactly as before.

---

## Filter Gate

The filter gate is an optional second step that can suppress email alerts based on the analysis. It sends the analysis summary to Claude with a yes/no boolean question. If the answer is NO, the alert is not sent. Results are always saved to parquet regardless.

### Why use it

Every analysis costs money, so the default is to **always send the alert** after analysis (filter disabled). But if you want Claude to act as a gatekeeper - for example, only alerting on genuine signals rather than noise - you can enable the filter.

### Enabling the filter

Add two fields to your saved search:

```yaml
analyzer_filter_enabled: true
analyzer_filter_question: "Based on this analysis, is there a genuine actionable trading signal that warrants immediate attention?"
```

Or via the API:

```bash
curl -X PUT http://localhost:5111/api/ss/volume_spikes \
  -H "Content-Type: application/json" \
  -d '{
    "analyzer_filter_enabled": true,
    "analyzer_filter_question": "Based on this analysis, is there a genuine actionable trading signal that warrants immediate attention?"
  }'
```

### Writing good filter questions

The filter question must be a **clearly boolean, singular question** - one that has a definitive YES or NO answer. Claude is instructed to respond with exactly one word.

**Good questions:**

- "Is this volume spike likely driven by genuine new information rather than arbitrage?"
- "Does this analysis indicate a pattern that requires immediate human review?"
- "Based on the confidence scores, should this alert be escalated to the trading desk?"

**Bad questions (avoid):**

- "What do you think?" - open-ended, not boolean
- "Should we maybe consider alerting?" - hedging language invites ambiguity
- "Is this interesting and also actionable and maybe important?" - multiple questions

### Filter behaviour

| Scenario | Result |
|----------|--------|
| Filter disabled (default) | Alert always sent |
| Filter enabled, Claude answers YES | Alert sent |
| Filter enabled, Claude answers NO | Alert suppressed |
| Filter enabled, Claude gives ambiguous answer | Alert sent (fail-open) |
| Filter enabled, API call fails | Alert sent (fail-open) |
| Filter enabled, budget exhausted | Alert sent (fail-open) |

The filter always uses the triage model (Haiku) for cost efficiency - it's a simple yes/no question that doesn't need the primary model.

> **Fail-open principle:** The filter never silently suppresses alerts due to errors. If anything goes wrong, the alert is sent. You'll see the filter result in the logs.

### Saved search fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `analyzer_prompt` | No | `""` | Name of the analyzer prompt to use. Empty = no analysis. |
| `analyzer_filter_enabled` | No | `false` | Enable the boolean filter gate after analysis. |
| `analyzer_filter_question` | No | `""` | The yes/no question to evaluate. Required when filter is enabled. |

---

## History Store

Every Claude API call made anywhere in SpeakesQuery - alert groups, scheduled-search analyzers, batch submissions, the Test Claude button - passes through a single wrapper (`analyzers/claude_client.call_messages_create`) that:

- Retries on transient errors (connection, 429, 5xx) with exponential backoff, configurable via `claude_retry_attempts` and `claude_retry_initial_backoff_seconds`. **`APITimeoutError` is deliberately non-retryable** - retrying a timeout just fires another attempt against the same ceiling and burns budget. The correct response to a timeout is to raise `claude_request_timeout_seconds`, not to add retries. Caught 2026-04-21 when the first Daily Opportunity Brief dispatch burned 4 × 120s = 8 minutes hitting the same wall before giving up.
- Enforces a hard timeout (`claude_request_timeout_seconds`, default **600s**, ceiling 3600s). Raised from 120s on 2026-04-21 - a `web_search_20250305`-enabled analyst brief with 10+ tool invocations legitimately takes 2–5 minutes. If you see `APITimeoutError` on a specific AG, raise this setting (Settings → Claude Analyzer → Request timeout) rather than debugging retry policy.
- Records each attempt to two places:
  - **`claude_api_history.sqlite`** (project root, NOT inside `indexes/`): full request + response JSON, gzip-compressed, with tokens, cost, latency, error class, stop reason. This is your audit ledger.
  - **`indexes/logs/claude_api/*.parquet`**: lightweight metadata row for SPQL-based cost queries, including the `headroom_path` route column (`headroom` / `direct` / `direct-fallback`). See [logging](14_logging.md).

The history DB lives outside `indexes/` so the automated cleanup never touches it. Retention is manual - back up and truncate when the file gets uncomfortable.

Note there are **two** history stores at the project root, split by dispatch path: Claude-native calls (everything through `call_messages_create`) land in `claude_api_history.sqlite`; router-dispatched calls (everything through `analyzers.llm_router.call_llm` - the `| llm` pipes and any alert group with a `model_id` set) land in `llm_call_history.sqlite`, which doubles as the content-hash response cache that makes idempotent `| llm` re-runs free. Both live outside `indexes/` and are never auto-cleaned.

### Headroom compression proxy

When the `global_use_headroom_default` setting is turned on (default: off), alert-analysis Claude calls - both the scheduled-search analyzer and alert groups - route through **Headroom**, an optional self-hosted compression proxy: a drop-in Anthropic Messages-API endpoint that strips low-information tokens from request bodies to cut input-token cost before forwarding to `api.anthropic.com`. Only enable this if you run a Headroom instance. Your Anthropic key passes through unchanged - the proxy holds none. From SpeakesQuery's side the whole decision is which `base_url` the Anthropic client is built with.

**Configuration** (`analyzers/headroom.py` owns the resolution):

- `global_use_headroom_default` (default `false`) - the global on/off default.
- `headroom_proxy_url` (default `http://localhost:8787`) - the proxy endpoint; env `HEADROOM_PROXY_URL` wins over the setting, which wins over the built-in default. A blank setting falls through to the default rather than breaking routing.
- Env `HEADROOM_DISABLE=1` - operational kill switch; forces every call direct regardless of settings.

**Fail-open behavior**: if the proxy is unreachable (connection error, timeout, or a proxy-side 502/503/504) the wrapper immediately retries the same call direct against Anthropic and logs `headroom_path=direct-fallback` - the failover does **not** consume the retry budget, so it fires even with zero retries configured. A genuine Anthropic 4xx is a real error and does NOT trigger failover. The proxy can never break analysis.

**Scope**: alert groups can override per-AG via the `use_headroom` tri-state (`true` / `false` / `null`=inherit); the scheduled-search analyzer follows the global default. `| llm` pipes, the patch drafter, batch submissions, and the Test Claude button always go direct. Full details in [Alert Groups → Headroom routing](12_alert_groups.md#headroom-routing-use_headroom).

### Model registry and LLM router

The analyzer's Claude-only wrapper sits inside a larger provider-agnostic layer:

- **Model registry** (`model_store.py`) - each `models/<id>.yaml` records a model's provider, underlying model name, endpoint, per-token costs, `max_output_tokens`, timeout, and an optional `sampling` block. Default templates ship in `default_models/` and are seeded missing-only, never overwriting user edits.
- **LLM router** (`analyzers/llm_router.py`) - `call_llm(model_id, ...)` looks the id up in the registry, picks the provider transport, and returns a uniform `LLMResponse`. Providers: `anthropic` (delegates to `call_messages_create`, preserving retry / budget / history capture), `ollama`, and `lmstudio` (OpenAI-compatible Chat Completions wire shape for self-hosted servers); `gemini` is a stub that fails loud until SDK demand surfaces.

The router is what powers the `| llm` family of SPQL pipes (see [LLM Pipes](18_llm_pipes.md)) and lets an alert group set `model_id` to route its analysis through a $0 local LAN model instead of the Claude API (see [Alert Groups → Local-model dispatch](12_alert_groups.md#local-model-dispatch-model_id)). The per-search analyzer described in this document remains Claude-native.

### Inspecting history

REST endpoints for the SQLite store:

```bash
# List most recent calls (payloads omitted for speed)
curl "http://localhost:5111/api/claude-history?limit=20"

# Include request + response bodies in the response
curl "http://localhost:5111/api/claude-history?limit=5&payloads=1"

# Filter by source / group_name / status
curl "http://localhost:5111/api/claude-history?source=alert_group&status=error"

# One record with decoded payloads
curl "http://localhost:5111/api/claude-history/<request_id>"

# Aggregate stats (optional since_epoch filter)
curl "http://localhost:5111/api/claude-history/stats?since=1776513600"
```

### Managing disk usage

The DB grows forever by default - that's intentional, so you never lose a costly call. When it gets too big:

```bash
# Delete rows older than a cutoff epoch + VACUUM to reclaim space
curl -X POST http://localhost:5111/api/claude-history/vacuum \
  -H "Content-Type: application/json" \
  -d '{"older_than_epoch": 1776000000}'
```

Back up the file first (`cp claude_api_history.sqlite claude_history.$(date +%F).sqlite`) - prunes cannot be undone.

Set `claude_history_retain_payloads: false` in `global_settings.yaml` to keep metadata without storing request/response JSON - saves space at the cost of forensic detail.

---

## Cost Controls

The analyzer includes multiple layers of cost protection:

### Gate logic (pre-API)

Before making any API call, these checks run in order. If any fails, the call is skipped:

1. **API key present** - no key = no calls
2. **Non-empty results** - empty result sets are skipped
3. **Budget kill switch** - if daily spend exceeds the configured budget, all calls stop
4. **Minimum liquidity** - if all rows fall below the liquidity threshold, the call is skipped

### Model routing

Most routine runs use the triage model (Haiku: $1/$5 per MTok). The primary model (Sonnet: $3/$15 per MTok) is only used when the data warrants it - specifically, when any row's `spike_multiple` exceeds the configured threshold.

### Prompt caching

The system prompt is the same structure for every call. Caching is enabled by default - the first call writes the cache (1.25x input cost), and subsequent calls within the 5-minute TTL read from cache (0.1x input cost). At 400 calls/day in rapid batches, virtually all calls are cache hits.

### Daily budget

The `claude_analyzer_daily_budget_cents` setting (default: 50 cents/day) is a hard ceiling. When 80% of the budget is consumed, a warning is logged. When 100% is reached, all subsequent calls are skipped until the next day. The budget resets at midnight (local time).

### Cost estimate

At 100 searches x 4 runs/day with the default controls:

| Scenario | Monthly Cost |
|----------|-------------|
| Realistic (60% skip, 70/30 Haiku/Sonnet, cache on) | ~$5 |
| Conservative (no skips, all Sonnet, cache on) | ~$45 |
| Maximum (no skips, all Sonnet, no cache) | ~$55 |

The daily budget default of 50 cents means the hard cap is **$15/month** regardless of call volume.

---

## Settings Reference

All analyzer settings are managed through the **Settings** page or via the `PUT /api/settings` endpoint.

| Setting | Default | Description |
|---------|---------|-------------|
| `claude_analyzer_enabled` | `false` | Master switch. Pipeline unchanged when false. |
| `claude_analyzer_boilerplate_prompt` | `""` | System-level prompt prepended to every analysis call. |
| `claude_analyzer_model_primary` | `claude-sonnet-4-6` | Model for CRITICAL/HIGH analysis. |
| `claude_analyzer_model_triage` | `claude-haiku-4-5-20251001` | Model for MODERATE/initial triage and filter gate. |
| `claude_analyzer_max_output_tokens` | `1024` | Hard cap on Claude response length. |
| `claude_analyzer_max_input_rows` | `20` | Truncate result sets beyond this for the API call. |
| `claude_analyzer_enable_cache` | `true` | Enable prompt caching (cache_control ephemeral). |
| `claude_analyzer_enable_batch` | `false` | Batch API - async processing at 50% reduced cost. |
| `claude_analyzer_batch_poll_interval_minutes` | `5` | How often the batch poller checks for completed results (1-60 min). |
| `claude_analyzer_daily_budget_cents` | `50` | Kill switch: stop calling API if daily spend exceeds this. |
| `claude_analyzer_spike_threshold` | `10.0` | spike_multiple above this routes to primary model. |
| `claude_analyzer_min_liquidity` | `5000.0` | Skip markets below this liquidity. |
| `claude_analyzer_mv_truncate_limit` | `5` | Max distinct values per token before truncation. |

---

## Example: End-to-End Workflow

### 1. Create an analyzer prompt

```bash
curl -X POST http://localhost:5111/api/analyzer-prompts/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "market_spike_analysis",
    "description": "Analyze Polymarket volume spikes",
    "prompt_text": "You are analyzing results from \"$scheduled_search_name$\" which ran at $execution_time$ and returned $result_count$ rows.\n\nMarkets: $question$\nSpike multiples: $spike_multiple$\nLiquidity: $liquidity$\n\nRespond with JSON: {alert_priority, summary, actionable_markets: [{question, position, confidence, reasoning, estimated_roi}], pattern_detected, cross_reference_needed}"
  }'
```

### 2. Create a saved search with the prompt attached

```bash
curl -X POST http://localhost:5111/api/ss/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "polymarket_spikes",
    "query": "index=\"polymarket/markets.parquet\" earliest=\"-6h\" | where spike_multiple > 2",
    "cron_schedule": "0 */6 * * *",
    "lookback": "-6h",
    "email_address": "alerts@example.com",
    "analyzer_prompt": "market_spike_analysis",
    "analyzer_filter_enabled": true,
    "analyzer_filter_question": "Is there at least one market with a genuine actionable signal above 70% confidence?"
  }'
```

### 3. What happens at each scheduled run

1. The query runs and returns (say) 15 rows of markets with volume spikes
2. The analyzer prompt tokens are resolved:
   - `$scheduled_search_name$` → `"polymarket_spikes"`
   - `$result_count$` → `"15"`
   - `$question$` → `"Arsenal EPL", "Iran NPT", "Scheffler Masters", "BTC 100k", "Fed rates", ... [+] 10 TRUNCATED`
3. The resolved prompt + full JSON of 15 rows are sent to Claude
4. Claude returns: `{"alert_priority": "HIGH", "summary": "...", "actionable_markets": [...]}`
5. The filter question is evaluated - Claude answers `YES`
6. The email alert is sent with the analysis summary appended

### 4. When the filter blocks

If Claude's filter answer is `NO`, the email is suppressed but:
- The query results are still saved to parquet (no data loss)
- The analysis result is logged (you can audit what was filtered)
- The filter decision appears in the application logs

---

## Troubleshooting

### "Claude analysis skipped: no_api_key"

No API key is stored in the credential vault. Get a key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys), then go to Settings → Claude Analyzer → API Key and save it, or use the `POST /api/settings/analyzer-key` endpoint.

### "Claude analysis skipped: budget_exceeded"

The daily budget has been reached. Either wait until tomorrow (budget resets at midnight) or increase `claude_analyzer_daily_budget_cents` in Settings.

### "Claude analysis skipped: below_min_liquidity"

All rows in the result set have a `liquidity` value below the configured `claude_analyzer_min_liquidity` threshold. Lower the threshold in Settings or adjust your query to filter out low-liquidity markets earlier.

### "Analyzer prompt 'X' not found"

The saved search references a prompt name that doesn't exist. Create the prompt first, or update the saved search's `analyzer_prompt` field.

### "Failed to parse response as JSON"

Claude returned text that doesn't match the expected JSON structure. This can happen with unusual data. The raw response is stored in the analysis result for debugging. Consider refining your prompt to be more explicit about the expected output format.

---

## Batch API

When enabled, the analyzer submits analysis requests to the Anthropic Message Batches API instead of making synchronous calls. Batch API requests are processed asynchronously (typically within 1-24 hours) at **50% reduced cost**.

### How it works

1. When `claude_analyzer_enable_batch` is `true`, analysis requests are submitted via `client.messages.batches.create()` instead of `client.messages.create()`
2. The analyzer returns immediately with `status="batch_pending"` and the request metadata is stored in the `batch_requests` table in `analyzer_results.sqlite`
3. A background poller runs on an interval (default: every 5 minutes) checking for completed batches
4. When a batch completes, the poller parses the results, records budget usage (at the 50% batch rate), runs any deferred filter gates, and stores the analysis result

### Enabling batch mode

On the **Settings** page, toggle **Enable Batch API** to `true`. Optionally adjust the poll interval:

```bash
curl -X PUT http://localhost:5111/api/settings \
  -H "Content-Type: application/json" \
  -d '{
    "claude_analyzer_enable_batch": true,
    "claude_analyzer_batch_poll_interval_minutes": 5
  }'
```

### Fallback behaviour

If a batch API submission fails (network error, SDK issue), the analyzer automatically falls back to the synchronous API path with a warning logged. This ensures analysis is never silently lost.

### Persistence

All analysis results, budget tracking, and batch request state are stored in `analyzer_results.sqlite` (separate from other databases). Budget tracking survives process restarts and is shared across instances via atomic SQLite upserts.

| Table | Purpose |
|-------|---------|
| `analyzer_results` | Full analysis outcomes (status, priority, summary, cost, filter result) |
| `analyzer_budget` | Daily token usage and cost tracking |
| `batch_requests` | Pending/completed batch request state with deferred filter settings |

---

## What's Next

- **[Application Guide](06_application_guide.md)** - Step-by-step UI instructions for all tabs
- **[Advanced Features](04_advanced.md)** - Saved searches, macros, and other advanced capabilities
- **[API Reference](10_api_reference.md)** - Complete endpoint documentation
- **[Email Setup](07_email_setup.md)** - Configure SMTP for alert delivery
