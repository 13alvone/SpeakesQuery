# Alert Groups - Multi-Search Claude API Dispatch

An alert group is a named collection of saved searches (up to the configured `alert_group_max_feeders` cap - default 10, allowed 2–100) whose most recent cached results are serialized together and dispatched to the Claude API in a single call, accompanied by your custom prompt instructions. The response is delivered as a branded HTML email through the existing email alert channel.

> **Key principle:** Alert groups are additive. They do not modify the single-search Claude analyzer path. They reuse the existing API integration, scheduler, and email system.

---

## Saved Search Purpose

Saved searches carry a `purpose` field that drives UI and validation:

- **`standalone`** (default) - runs on its own cron schedule AND sends its own email. Requires a valid email address. The historical behaviour.
- **`alert_group_feeder`** - data-only feeder for one or more alert groups. Still needs its own cron so fresh data lands before the AG fires, but never sends email of its own. `send_email` is forced to `no`, a sentinel `noreply@speakesquery.local` fills the email address, and the UI hides email/analyzer/trigger fields.

### Auto-toggle at AG save time

When an AG is created or updated with `search_names` that reference existing `standalone` saved searches, each of those searches is **auto-toggled** to `alert_group_feeder` at that moment. The toggle is idempotent (no-op if already a feeder) and emits an `auto_toggle_to_feeder` row to `indexes/logs/config/*.parquet` with `source="alert_group:<name>"` for audit.

This means you can create saved searches naturally with default `standalone` semantics; the moment an AG references them, their purpose flips automatically. No manual reconfiguration required.

## How It Works

1. A saved search executes on its cron schedule and caches results as Parquet files
2. An alert group fires (on its own cron schedule or via manual trigger)
3. The dispatcher loads the most recent cached result for each search in the group
4. Results are row-capped and serialized to JSON (or CSV)
5. Your prompt instructions are prepended, followed by metadata and the serialized data blocks
6. The assembled prompt is sent to Claude with web search enabled
7. Claude's response is wrapped in a branded HTML email (with SpeakesQuery logo, metadata bar, and styled body) and sent to all configured recipients

---

## Concepts

**Saved search** - An existing SpeakesQuery scheduled query that produces cached Parquet results. See [Application Guide](06_application_guide.md) for saved search setup.

**Prompt instructions** - Free-form text you write directly in the alert group. This is sent to Claude as the leading instruction, followed by the search result data blocks. No template variables needed - just write what you want Claude to do.

**Alert group** - A named collection of saved search references (1 up to `alert_group_max_feeders`, default 10), prompt instructions, an optional cron schedule, and a row cap. Stored as YAML in `alert_groups/`.

**Dispatch / run** - One execution of an alert group: serialize results, call Claude, send HTML email, log the outcome.

---

## Shipped Default Alert Groups

SpeakesQuery ships eleven complementary default alert groups under `alert_groups/` with matching boilerplate prompts in `boilerplate_prompts/` and feeders in `default_saved_searches/`. Eight run daily; three run weekly. They are staggered across UTC so API / cost budgets do not collide.

### `daily_opportunity_brief`

Company / ticker-level idiosyncratic edge. Fires `30 11 * * *` (11:30 UTC). 10 feeders covering Polymarket high-probability contracts, Kalshi × Polymarket arbitrage, Polymarket volume spikes, CoinGecko volume anomalies, SEC 8-K / Form-4 filings, Reddit ticker buzz, mega federal contract awards, FRED fear gauges, 72-hour earnings calendar, and unusual options activity. Targets prediction markets, crypto spot, equities, options, commodities, FX - TOP 5 picks with ≥75% conviction and ≥8h runway.

### `global_macro_risk_brief`

Country / commodity / geopolitical regime-shift edge. Fires `15 13 * * *` (13:15 UTC, post-EU-open, pre-US-open). 10 feeders covering USGS significant earthquakes, NOAA severe weather alerts, NHC active tropical cyclones, USGS volcanic alert levels, GDELT geopolitical conflict events, World Bank country growth indicators, FRED global central bank policy rates, FRED commodity prices, FRED FX + real yields + breakeven, and OECD Composite Leading Indicators. Targets country ETFs (EWZ / INDA / FXI / EWW / EZA / ...), commodity ETFs (USO / UNG / GLD / COPX / CORN / ...), currency ETFs (UUP / FXE / FXY / ...), and thematic macro equities (reinsurers, defence, miners, airlines, shippers) - TOP 5 picks with ≥75% conviction and typically 1–30 day horizons.

### `fx_rate_brief` (Wave 1)

Currency-pair and rate-divergence edge. Fires `45 6 * * *` (06:45 UTC, pre-London open). 5 feeders covering trade-weighted USD regime (Broad / DXY-equivalent / EM dollar), G10 USD majors regime, EM-USD stress (MXN / BRL / INR), G4 central bank policy rates and yields, and pairwise G10 carry-trade attractiveness. Targets currency ETFs (UUP / UDN / FXE / FXY / FXB / FXC / FXA / FXF / FXM / BZF / CYB), inverse FX ETFs, FX options, and rate-sensitive equity proxies (TLT, EM ETFs, gold via GLD) - TOP 5 picks with ≥75% conviction.

### `sports_betting_edge_brief` (Wave 1)

Pure +EV value-betting edge across the major US leagues. Fires `30 15 * * *` (15:30 UTC, ~90 min before the MLB / NBA late-afternoon slate locks). 5 feeders covering current sportsbook line snapshots across the major US books (The Odds API), book-disagreement / sharp-money divergence, ESPN league-wide injury reports, Kalshi sports contracts, and Polymarket sports markets. Targets moneyline, spreads, totals, and prediction-market arb. TOP 5 picks with ≥75% conviction at the offered price (i.e., a clear mispricing, not a coin-flip). Bet sizing reported in Kelly tiers (SMALL / MEDIUM / LARGE) - the brief never recommends a dollar size.

### `energy_grid_intelligence_brief` (Wave 1)

Energy and grid-fuel-mix edge. Fires `45 14 * * *` (14:45 UTC, ~30 min after the EIA Wednesday WPSR / Thursday NGSR releases; daily run captures grid + price drift on other days). 5 feeders covering EIA Weekly Petroleum Stocks, EIA Weekly Natural Gas Storage by region, EIA daily electricity demand by US balancing authority, EIA daily generation mix by fuel type, and FRED energy commodity price regime. Targets oil ETFs (USO / BNO / UCO / SCO), gasoline (UGA), nat gas (UNG / BOIL / KOLD), utilities (XLU), renewables (TAN / FAN / ICLN / NLR), and energy equities (XLE / XOP / XOM / CVX / OXY / VLO / MPC) - TOP 5 picks with ≥75% conviction and typically 3–30 day horizons.

### `crypto_deep_signals_brief` (Wave 2)

Crypto regime-shift edge. Fires `0 2 * * *` (02:00 UTC, overnight regime catcher; crypto never sleeps and most regime shifts land while US sleeps). 5 feeders covering DeFi Llama stablecoin supply, DeFi protocol TVL movers, yield opportunities across stablecoin/ETH/BTC pools, CoinGecko centralised exchange volumes, and BTC + altcoin market dominance. Targets crypto spot (BTC / ETH / SOL / etc.), liquid altcoins, DeFi protocol tokens (UNI / AAVE / MKR), DeFi yield positions, and crypto-adjacent equities (COIN / MSTR / MARA). Distinct from DOB's crypto anomaly slice - this brief is regime-focused. **Zero new ingestion scripts** - all 5 feeders are SPQL projections from existing crypto indexes.

### `politics_policy_prediction_brief` (Wave 2)

Politics + policy edge. Fires `30 21 * * *` (21:30 UTC, post-US market close, before Asia trading). 5 feeders covering Kalshi politics contracts, Polymarket politics markets, Congress.gov HIGH/MEDIUM-importance bills (chamber passage, signed-into-law, committee report, floor amendment), Federal Register significant rules and presidential documents, and Kalshi economy/Fed/CPI/GDP/jobs contracts (live macro-policy consensus). Targets prediction markets (Kalshi/Polymarket politics), sector ETFs sensitive to policy outcomes (XLV / IBB on healthcare; XLE / TAN on energy; XLF / KRE on financial regulation; ITA / XAR on defense; SOXX / SMH on chip exports), and policy-sensitive single equities (LMT, UNH, XOM, individual chip stocks).

### `public_health_pharma_brief` (Wave 2)

Pharma / biotech / public-health catalyst edge. Fires `30 3 * * *` (03:30 UTC, overnight slot before Asia; FDA / NIH / ClinicalTrials.gov updates land late-day US time). 5 feeders covering openFDA adverse-event safety signals (FAERS), FDA drug approvals + supplements, ClinicalTrials.gov Phase 3 status updates, openFDA active drug shortages, and Kalshi health/FDA/pandemic/vaccine prediction-market contracts. Targets pharma + biotech equities (REGN / BIIB / VRTX / NVAX), generic-drug manufacturers (TEVA / MYL / AMRX / ENDP) for shortage trades, healthcare ETFs (XLV / IBB / XBI / IHE), prediction markets, and options on the above.

### `science_forecasting_brief` (Wave 3)

Research-driven thematic edge with multi-week to multi-month horizons. Fires `0 5 * * *` (05:00 UTC, early-morning slot before European market open). 4 feeders covering open Metaculus forecasting questions sorted by community engagement, recent arXiv papers across quant categories (cs.AI / cs.LG / cs.CL / cs.CR / cs.NE / q-fin.* / q-bio.* / stat.ML), NIH-funded research grant clusters from the past 30 days, and high-event-count GitHub repos. Targets thematic equities + ETFs that capture capability shifts (NVDA / AMD / SMH / SOXX on AI papers; ICLN / TAN on energy research; biotech single-names on q-bio + NIH funding; data-center names on training-compute papers), prediction markets that mirror Metaculus questions, and options.

### `religion_cultural_prediction_brief` (Wave 3, weekly)

Religion + cultural-event prediction edge. Fires `0 18 * * 0` (Sundays 18:00 UTC) - weekly cadence reflects the slow, sparse signal in this domain. 4 feeders covering Kalshi religion contracts (papal, religious-event timing), Polymarket religion / cultural markets (papal succession, Hajj attendance, religious leadership transitions), Wikipedia pageview momentum on a curated list of religious figures + institutions + cultural events, and GDELT global news filtered to religion / sectarian themes. Targets prediction markets, defensive equities (gold + safe-haven on sectarian escalation), and culture-themed sectors. **Empty briefs are an acceptable outcome - religion-domain signal is sparse, and the prompt explicitly forbids fabrication.**

### `civilization_pulse_brief` (Wave 3, weekly)

Slow-arc thematic attention edge. Fires `0 12 * * 0` (Sundays 12:00 UTC). 4 feeders covering GDELT global news themes aggregated by tension theme + severity, World Bank country growth indicators with investability tags, top Hacker News stories from past 7 days, and top Wikipedia pageviews across 8 major-language editions (English, Spanish, German, French, Japanese, Russian, Chinese, Portuguese) with category classification. Targets thematic equity ETFs (AIQ / BOTZ / ROBO on AI attention; ICLN / TAN on climate; KSA / INDA / EWZ on emerging-market attention), country ETFs derived from World Bank growth tiers, prediction markets on cultural / political / macro contracts, and large-cap single names with concentrated thematic exposure.

All eleven briefs write their picks to `indexes/IMMUTABLE/ag_picks/*.parquet` with `alert_group=<name>` so per-AG history and reserved-ideas dedup loops stay cleanly partitioned. They share the same mandatory structured JSON tail schema (`idea_id`, `instrument_type`, `instrument_id`, `direction`, `conviction_pct`, `expected_return_pct`, `position_size_tier`, `entry_price`, `suggested_buy_epoch`, `suggested_sell_epoch`, `hold_hours`, `take_profit_price`, `stop_loss_price`, `exit_catalyst`, `thesis`, `source_signals`, `correlation_cluster`, `short_squeeze_risk`) so downstream backtesting and dedup work uniformly across briefs.

### `github_hot_repos_brief` (Slice B, 2026-06-23 - local model, not a trading brief)

A daily developer digest of the 10 repositories newly trending on GitHub, each with a one-line "why it's hot" written by the **local LAN model** (`llamacpp-qwen35-122b-a10b`, $0/token) - the first alert group to use the Slice A [`model_id`](#local-model-dispatch-model_id) field instead of the Claude API. Unlike the eleven trading briefs above, it sets **no `output_kind`** and asks for a plain-text digest (no JSON tail), so it never writes to `indexes/IMMUTABLE/ag_picks/`. No-repeat-for-30-days is handled entirely by its feeder (`github_hot_repos_today`) via a first-seen dedup over the accumulating `github/trending` ingestion parquets - no journal needed. Fires `30 8 * * *` America/Los_Angeles (08:30, 30 minutes after the 08:00 `github_trending_repos` ingestion). Ships **disabled** with no recipient - enable it and set your email to use it.

### `ai_paper_diffs_brief` (Slice C1, 2026-06-23 - local model, diff digest)

Your personal daily "what's new in AI" journal: an ELI5 digest of papers newly **added** to five curated AI-paper GitHub lists (agent papers, awesome-AI, neuro-AI, ML-papers-explained, papers-of-the-week) plus Hugging Face's Daily Papers feed, written by the **local 122B** ($0). Uses [`model_id`](#local-model-dispatch-model_id) plus `skip_on_empty` (quiet days with no new papers skip cleanly - no email, no error, no breaker trip). No `output_kind` / no JSON tail, so it never touches `indexes/IMMUTABLE/ag_picks/`. The "diff" - only newly-added papers surface - is handled by the feeder (`ai_papers_new_today`) via a first-seen dedup over the accumulating `ai_papers/*` parquets (keyed by arxiv id, so a paper in a GitHub list AND on Hugging Face cross-dedups). Fires `30 7 * * *` America/Los_Angeles (07:30, after the 07:00 `ai_papers_github_lists` ingestion). The first digest is large (cold start - every current paper is "new"); after that it's a daily diff. Ships **disabled** with no recipient. (dair-ai/AI-Papers-of-the-Week's README is a newsletter pointer, so it contributes ~0 entries; the other four lists carry the signal.)

### Stagger map (UTC)

**Daily briefs:**

| UTC | Brief | Theme |
|-----|-------|-------|
| 02:00 | `crypto_deep_signals_brief` | Crypto overnight regime |
| 03:30 | `public_health_pharma_brief` | Pharma + biotech catalysts |
| 05:00 | `science_forecasting_brief` | Research / forecasting edge |
| 06:45 | `fx_rate_brief` | FX & rate-differential, pre-London open |
| 11:30 | `daily_opportunity_brief` | Company / ticker-level edge |
| 13:15 | `global_macro_risk_brief` | Country / commodity / geopolitical macro |
| 14:45 | `energy_grid_intelligence_brief` | Energy & grid intelligence |
| 15:30 | `sports_betting_edge_brief` | Sports betting +EV |
| 21:30 | `politics_policy_prediction_brief` | Politics + policy + agency rulemaking |

**Weekly briefs (Sundays):**

| UTC | Brief | Theme |
|-----|-------|-------|
| 12:00 | `civilization_pulse_brief` | Slow-arc thematic attention shifts |
| 18:00 | `religion_cultural_prediction_brief` | Religion + cultural-event prediction |

---

## Quickstart

### 1. Create an alert group (UI)

Navigate to the **Alert Groups** tab in the SpeakesQuery UI:

1. Click **+ New Alert Group**
2. Enter a name and description
3. Select up to the configured cap of saved searches (`alert_group_max_feeders`, default 10; Ctrl/Cmd-click for multiple)
4. Write your prompt instructions - e.g., *"Analyze the following market data and identify the 5 highest-conviction opportunities with probability, expected value, and confidence tier."*
5. Optionally set a cron schedule, max rows, and email addresses
6. Click **Save Alert Group**

### 2. Create an alert group (API)

```bash
curl -X POST http://localhost:5111/api/alert-groups/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "daily_market_brief",
    "description": "Morning prediction market summary",
    "search_names": ["polymarket_scanner", "kalshi_scanner"],
    "prompt_text": "Analyze the following prediction market data and provide a summary of the 5 highest-conviction opportunities.",
    "schedule": "0 6 * * *",
    "max_rows": 200,
    "email_address": "analyst@example.com, team@example.com"
  }'
```

### 3. Run manually

```bash
curl -X POST http://localhost:5111/api/alert-groups/daily_market_brief/run
```

The response includes the full Claude output, token usage, and cost.

### 4. Schedule runs automatically

The `schedule` field accepts any valid cron string. If set, the group is automatically registered with the APScheduler on startup. If omitted or empty, the group can only be triggered manually.

### 5. Schedule in a market timezone (DST-safe)

Cron expressions are interpreted in the AG's `timezone` field - any IANA zone name like `America/New_York`, `Europe/London`, `Asia/Tokyo`. APScheduler handles spring-forward and fall-back automatically, so a cron like `30 10 * * 1-5` with `timezone: America/New_York` fires at 10:30 ET every weekday year-round (14:30 UTC in EDT, 15:30 UTC in EST) without any manual edits.

If `timezone` is omitted or blank, it defaults to `UTC` - every alert group written before 2026-04-27 keeps its existing behavior. Set it explicitly when the cron's intent depends on a real-world clock (market open/close, government deadline, news cycle):

```yaml
name: my_pre_open_brief
schedule: "0 9 * * 1-5"
timezone: America/New_York   # 9:00 AM ET → fires 30min before NYSE open
```

The next-run time displayed in the UI table converts the AG's TZ to your browser's local clock, so you always see "May 1, 6:00 AM" in *your* time even if the AG fires in another zone.

> **Why this matters:** before this field existed, a cron `30 14 * * 1-5` was UTC-only, which meant 10:30 AM ET in summer (EDT) but **9:30 AM ET in winter (EST)** - a one-hour drift twice a year that silently desynced "morning brief" from market open. With `timezone: America/New_York`, the wall-clock is stable and the description stays honest.

---

## Configuration Reference

### Alert group fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | - | Unique identifier. Letters, digits, spaces, hyphens, underscores, periods. |
| `description` | string | no | `""` | Human-readable description. |
| `search_names` | list | yes | - | 1 to `alert_group_max_feeders` saved search names (global setting, default 10, allowed 2–100). Each must exist in `saved_searches/`. |
| `prompt_text` | string | yes | - | Free-form instructions sent to Claude before the data blocks. |
| `schedule` | string | no | `""` | Cron string (e.g. `0 6 * * *`). Empty = manual only. |
| `timezone` | string | no | `"UTC"` | IANA zone the cron is interpreted in (e.g. `America/New_York`, `Europe/London`). DST handled automatically. Bare offsets like `-07:00` are rejected. See [Schedule in a market timezone](#5-schedule-in-a-market-timezone-dst-safe). |
| `error_email_disabled` | bool | no | `false` | If `true`, the dispatcher skips the failure-alert email entirely for this AG - short-circuits BEFORE consulting `admin_error_email` or the global `alert_group_failure_email_to` fallback. Use for AGs whose owners watch dashboards instead of inboxes. The opt-out is logged at INFO so operators can see why no email went out. |
| `admin_error_email` | string | no | `""` | Admin-only recipient for this AG's failure / diagnostic notices (Wave 5). When set, wins over the global `alert_group_failure_email_to` fallback. Validated as an email only when non-empty. Failure notices **never** fall through to the customer-facing `email_address`. See [Failure alerts](#failure-alerts). |
| `max_rows` | int | no | `200` | Per-search row cap before serialization. Range: 1–10000. |
| `email_address` | string | no | `""` | Recipient email(s). Comma or semicolon separated for multiple recipients. Required when `delivery_mode` is `prompt_only`. |
| `disabled` | bool | no | `false` | If true, scheduled runs are skipped. Manual runs still work. |
| `delivery_mode` | string | no | `"api"` | `"api"` (default) = call Claude and email the analyst brief. `"prompt_only"` = budget-friendly mode; build the prompt and email it to you **without** calling Claude. See [Budget-Friendly Mode](#budget-friendly-mode-email-the-prompt-instead). |
| `model_id` | string | no | `""` | Optional registry model id (e.g. `llamacpp-qwen35-122b-a10b`). When set, the dispatcher routes this AG's analysis through the provider-agnostic LLM router to that model - typically a **local LAN model** ($0/token) - instead of the Claude API. Empty = Claude (default). See [Local-model dispatch](#local-model-dispatch-model_id). |
| `skip_on_empty` | bool | no | `false` | When `true`, a run where all feeders return zero rows ends as **skipped** (no error, no failure email, no circuit-breaker trip) instead of an error. Use for diff-style AGs ("what changed today") that legitimately have nothing to report on a quiet day. |
| `use_headroom` | bool / null | no | `null` (inherit) | Tri-state override for routing this AG's Claude call through the **Headroom** compression proxy. `null`/absent = inherit the global `global_use_headroom_default` setting; `true` = force proxy; `false` = force direct Anthropic. Only affects Claude-routed AGs (no effect when `model_id` is set). See [Headroom routing](#headroom-routing-use_headroom). |

---

### Headroom routing (`use_headroom`)

**Headroom** is a local context-compression proxy (a drop-in Anthropic Messages-API endpoint) that strips low-information tokens from request bodies to cut input-token cost before forwarding to `https://api.anthropic.com`. Your Anthropic key is passed through unchanged - the proxy holds none. From SpeakesQuery's side, "use Headroom or not" is just which `base_url` the Anthropic client is built with.

Routing is decided per call, most-specific override wins:

```
effective use_headroom = per-AG use_headroom
                         else global_use_headroom_default   (default: false)
```

- **Global default** - Settings → Alert Groups → *Route alert analysis through Headroom proxy*, or the `global_use_headroom_default` setting (default `false`). Leave it off unless you self-host a Headroom instance; fail-open means a dead proxy costs a wasted connection attempt per call, not a broken analysis.
- **Per-AG override** - the `use_headroom` field (Edit AG → *Headroom Proxy* dropdown): *Inherit* / *Yes* / *No*.
- **Proxy URL** - `headroom_proxy_url` setting (default `http://localhost:8787`), overridden at runtime by the `HEADROOM_PROXY_URL` env var. If your proxy host's DNS name resolves to IPv6 but the proxy listens on IPv4 only, use the IPv4 literal.

**Fail-open (mandatory).** Headroom can never take down alert analysis. If a Headroom-routed call hits a connection-level failure (proxy unreachable / refused / reset / timeout / HTTP 502-504), the dispatcher automatically retries the **same** call against direct Anthropic and logs a warning. A genuine Anthropic 4xx (e.g. 401) does **not** fail over - it would also fail direct.

**Kill switches.** Set the global default to `false` (with no AG forcing yes), or set the env var `HEADROOM_DISABLE=1` - either forces every alert-analysis call direct, regardless of per-AG settings.

**Observability.** Each Claude attempt records its route in the `headroom_path` column of `indexes/logs/claude_api/*.parquet`: `headroom` | `direct` | `direct-fallback`. Query it alongside `input_tokens` to measure compression savings once compression is enabled:

```spl
index="indexes/logs/claude_api/*.parquet" | stats count avg(input_tokens) by headroom_path
```

The scheduled-search Claude analyzer also honors the global default (it has no per-search override yet).

---

### Local-model dispatch (`model_id`)

By default an alert group calls the **Claude API**, and the dispatcher gives Claude its `web_search` tool. Set `model_id` to a registry model (`models/<id>.yaml`) to route the AG through the provider-agnostic LLM router instead - most usefully a **local LAN model**, so the AG runs with zero cloud dependency at $0 per-token cost.

In the UI, pick it from the **Analysis Model** dropdown on the alert-group Edit form: *Claude API (default)* keeps the native Claude path (with `web_search`); selecting a registered local/self-hosted model (e.g. `llamacpp-qwen35-122b-a10b`) sets `model_id` to route through the router. Anthropic models aren't listed as alternatives there - the default option already covers Claude with `web_search`, whereas routing a Claude model by id would go through the router and lose it.

```yaml
# alert_groups/ai_paper_diffs.yaml
name: ai_paper_diffs
model_id: llamacpp-qwen35-122b-a10b   # local Qwen3.5-122B on your llama.cpp server
search_names: [ai_papers_feeder]
prompt_text: "ELI5 what changed in these AI-paper lists today…"
email_address: me@example.com
schedule: "0 9 * * mon-fri"
```

What changes on the local branch:

- **No `web_search` tool.** A local model serves a single-shot completion - it cannot use Anthropic's server-side search tool, and an MCP server does not change that (there is no agentic tool-use loop for the model to call into). Feed it fresh context at the **ingestion layer** (a feeder that fetches the pages) rather than expecting the model to browse.
- **Per-record timeout.** The model's `default_timeout_seconds` (e.g. 600s for the 122B) applies, not `claude_request_timeout_seconds` - local inference legitimately takes 1–5 minutes.
- **$0 budget.** `max_cost_usd_per_run` / `max_cost_usd_per_day` are no-ops for a local model (registry pricing is `0.0`/Mtok), so a free AG is never blocked by a cost cap.
- **Empty-response guard.** A reasoning model whose `<think>` trace loops past its token budget can return empty text; the dispatcher fails the run loud (failure email + circuit-breaker tick) instead of emailing a blank brief. Pin the model's anti-loop `sampling` (e.g. `presence_penalty`) in its registry record - see `model_store`.
- **History.** Local calls are recorded in `llm_call_history.sqlite` (via the router), not `claude_api_history.sqlite`.

A typo'd `model_id` is rejected at save time when the model registry is loadable; otherwise the dispatcher surfaces an `UnknownModel` error on the next run. Leave `model_id` empty to keep the Claude path unchanged.

---

## Prompt Instructions

Write your instructions as plain text - no special template syntax is required. Your text is sent to Claude as the leading instruction, followed by:

- Alert group name and UTC timestamp
- Number of searches included
- Each search result rendered as a labeled markdown block with code-fenced data

### Search block format

Each search result is automatically rendered as:

```
## Search: <search_name> (<row_count> rows, JSON)

```json
[{"col1": "val1", ...}, ...]
```
```

Multiple blocks are separated by horizontal rules (`---`).

### Example prompt instructions

> Analyze the following prediction market data and identify the 5 highest-conviction opportunities. For each, provide: market name, current probability, expected value, and confidence tier (High/Medium/Low). Conclude with a one-paragraph executive summary.

---

## Budget-Friendly Mode: Email the Prompt Instead

Every alert group has a `delivery_mode` field with two settings:

- **`api`** (default) - run the feeders, build the prompt, call Claude, email the analyst brief. Costs API tokens on every fire.
- **`prompt_only`** - run the feeders, build the prompt, and email you **the built prompt text itself**. No Claude API call. No cost. You paste the prompt into [Claude.ai](https://claude.ai) (or any other LLM) manually to complete the analysis.

Use this mode when:

- You're exploring a new alert group and don't yet trust the prompt quality enough to pay per fire.
- You want an archive of exactly what the scheduled API fire would have sent, without paying for a Claude call.
- You're on a tight budget and prefer to handle the analysis manually via your existing Claude.ai session.
- You're demoing the system and want to show the pipeline end-to-end without burning tokens.

### What you get by email

| Subject | Body | Attachment |
|---------|------|------------|
| `[SpeakesQuery PROMPT] <group> - <YYYY-MM-DD>` | Branded HTML email with a blue "Prompt-only delivery" banner, the built prompt rendered in a `<pre>` block (preserves fenced code / markdown tables for clean copy-paste), and a meta bar that explicitly shows `Mode: prompt-only (no API call, $0.00)` | `.md` file containing the full prompt text, so you can paste without picking it out of the HTML |

The subject uses `PROMPT` (not `REPORT`) so you can filter or route these emails separately from full analyst briefs in your mail client.

### Rules

- **`email_address` is required.** With `delivery_mode: prompt_only` the email *is* the delivery. The store rejects saves without it and returns an actionable error (not a silent cron fire that goes nowhere).
- **All other gates still apply.** Rate limit (`max_dispatches_per_day`, `min_interval_between_runs_hours`), circuit breaker, disabled flag, freshness checks, prompt-text gate, token-trim loop - all run exactly as in API mode, so the prompt you receive is identical to what the API path would have sent at that moment.
- **Cost gates are bypassed.** The per-AG cost budget (`max_cost_usd_per_run`, `max_cost_usd_per_day`) does not apply since there's no Claude spend.
- **Run status is `prompt_only`.** Runs are recorded in `alert_group_runs.sqlite` and `indexes/logs/alert_groups/*.parquet` with `status="prompt_only"`, `cost_usd=0.0`, `actual_tokens=0`, and the real `estimated_tokens` for audit.
- **UI signal.** The Alert Groups list shows a compact `PROMPT-ONLY` badge next to the group name so you can tell at a glance which groups are in budget mode.

### Switching modes

Toggle `delivery_mode` via the Alert Groups edit form, or hit the PUT endpoint:

```bash
curl -X PUT http://localhost:5111/api/alert-groups/<name> \
  -H "Content-Type: application/json" \
  -d '{"delivery_mode": "prompt_only"}'
```

The scheduler re-registers automatically on save - no restart required.

---

## Email Output

Alert group emails are sent as **multipart HTML + plain text**. The HTML version includes:

- **SpeakesQuery logo** at the top (the light-theme SVG logo from the app)
- **Alert group name** as a styled heading
- **Metadata bar** showing search count, estimated tokens, actual tokens, and cost
- **Claude's response** rendered with formatted headers, bullet lists, bold/italic, and code blocks
- **Footer** with generation attribution

All email clients receive a plain-text fallback for compatibility. The HTML formatting is applied consistently regardless of what prompt instructions you provide. Markdown tables in the response render as real HTML tables (2026-08-04).

### BLUF-first email digest (2026-08-04)

Raw analyst output from a web_search-enabled Claude run is an archive, not an email: it interleaves internal analysis notes, tool narration, and a machine-readable JSON tail around the actual brief. Set the optional per-AG field `email_digest_model_id` to a registry model (typically a $0 local model like `llamacpp-qwen35-122b-a10b`) and the dispatcher distills the raw response into a short, fixed-structure report before sending:

1. **BLUF** - 2-4 sentences, bottom line up front (action, pick count, stance)
2. **Today's Picks** - compact per-pick cards (omitted when zero picks)
3. **Key Context** - performance pulse, regime, notable rejections
4. **Watch Next** - resume conditions

Contract details:

- Pick extraction ALWAYS runs on the **raw** response - journaling to `indexes/IMMUTABLE/ag_picks/` is unaffected by the digest.
- The complete raw response ships as the `.md` attachment; the digest is only the inline body.
- The email subject gains the pick count (e.g. `... - 2 picks`) so the BLUF starts in the inbox list view.
- Digest failure (model down, empty output) falls back to the raw text minus the trailing JSON fence - never a lost brief. The digest call uses the same graduated retry as guard 4b.
- `alert_group_digest_max_tokens` (default 8192) caps the digest output. Keep it at 8192+ for thinking models - the reasoning trace counts against the cap and a starved trace returns empty content.

`options_edge_brief` ships with the digest enabled by default.

### Multiple recipients

The email address field accepts comma or semicolon-delimited addresses:

```
analyst@example.com, team-lead@example.com; alerts@company.com
```

All addresses receive the same branded HTML email.

---

## Scheduling

Alert groups use the same APScheduler instance as saved searches. Cron string format follows the standard five-field syntax:

```
┌───────────── minute (0–59)
│ ┌───────────── hour (0–23)
│ │ ┌───────────── day of month (1–31)
│ │ │ ┌───────────── month (1–12)
│ │ │ │ ┌───────────── day of week (0–6, Sun=0)
│ │ │ │ │
* * * * *
```

Groups without a schedule are not registered with the scheduler. They can only be triggered manually via `POST /api/alert-groups/<name>/run`.

Disabling a group (`disabled: true`) prevents scheduled runs but does not remove the schedule definition. Re-enabling resumes on the existing schedule.

### Best practices for scheduling saved searches used by alert groups

When a saved search exists **only** to feed data into an alert group (i.e., you never review its results independently), follow these guidelines:

1. **Schedule the saved search to run before the alert group.** If your alert group fires at `0 6 * * *` (6:00 AM), schedule the underlying saved searches at `45 5 * * *` (5:45 AM) to ensure fresh results are cached before dispatch.

2. **Use a comfortable buffer.** Allow at least 10–15 minutes between the saved search execution and the alert group schedule. Large queries or slow ingestion sources may need more.

3. **Avoid over-scheduling.** If the saved search only exists for the alert group, match its cron frequency to the alert group's schedule - not more frequent. Running a search every 5 minutes when the alert group only fires daily wastes compute and disk.

4. **Prefer overlapping lookback windows.** Set the saved search's lookback to be slightly wider than the cron interval. For a daily alert group, a saved search with `lookback=-2d` ensures no gaps even if one execution is missed or delayed.

5. **Disable the saved search's own email.** If the search is only feeding an alert group, leave its email address empty - the alert group handles the delivery. This prevents duplicate emails.

6. **Name searches clearly.** Use a short AG-identifier prefix so the list view groups by owner (shipped defaults use `dob_*` for Daily Opportunity Brief and `gmrb_*` for Global Macro Risk Brief). This also lets Feeder Health + reserved-picks dedup loops scope cleanly when multiple AGs share the same underlying index.

---

## Row Limits and Token Budget

### Row cap (`max_rows`)

Applied per-search before serialization. If a search returned 500 rows and `max_rows` is 200, only the first 200 rows are included. Default: 200. Enforced by `ResultSerializer.serialize()` via `df.head(max_rows)` and regression-tested in `tests/test_alert_group_robustness.py::TestRowCap`.

### Token estimation

Before calling the Claude API, the dispatcher estimates total tokens using a conservative heuristic: `estimated_tokens = len(json_string) / 3.5`. If the estimate exceeds the configured daily budget gate (derived from `claude_analyzer_daily_budget_cents` in Settings), rows are iteratively trimmed from the largest result until the payload fits.

### What happens when results are trimmed

The dispatcher logs a warning and halves the row count on the largest result, repeating until the estimate is under budget. The `estimated_tokens` field in the run log reflects the post-trim value. The original search data is never modified - trimming only affects the prompt payload.

---

## Run History

Every dispatch (manual or scheduled) is logged in two places:

1. **`alert_group_runs.sqlite`** - canonical audit SQLite table:

   | Field | Description |
   |-------|-------------|
   | `group_name` | Which alert group ran |
   | `triggered_at` | UTC timestamp |
   | `status` | `success`, `error`, `skipped`, `rate_limited`, `dry_run`, or `prompt_only` |
   | `searches_used` | JSON array of search names that were included |
   | `estimated_tokens` | Pre-flight token estimate |
   | `actual_tokens` | Actual tokens from Claude API response |
   | `cost_usd` | Estimated cost in USD |
   | `error_message` | Error detail (empty on success) |

2. **`indexes/logs/alert_groups/*.parquet`** - SPQL-queryable stream for trend analysis. Same fields plus `duration_ms` and `dry_run`. See [logging](14_logging.md) for schema details.

Query run history via the API:

```bash
curl "http://localhost:5111/api/alert-groups/runs?group_name=daily_market_brief&limit=10"
```

The alert groups list page surfaces the most recent run as a pill next to each group - click it to see the last ten attempts with error detail. A previously-silent Claude key misconfiguration now appears visibly on the list rather than disappearing into logs.

### Failure alerts

When a run ends in `error` status - a missing API key, a Claude outage, an SMTP failure, an empty result set, whatever - the dispatcher emails a plain-text notification to the admin so silent failures can't go unnoticed.

| Setting | Default | Notes |
|---------|---------|-------|
| `alert_group_failure_email_enabled` | `true` | Master switch. |
| `alert_group_failure_email_to` | `""` | **Global fallback** recipient. When blank, falls back to `smtp_from`, then `smtp_user`. The per-AG `admin_error_email` field (Wave 5) wins over this when set. |
| `alert_group_max_feeders` | `10` | Per-AG cap on saved-search references. Allowed range `2`–`100`; set on the Settings page. Applied at AG create/edit time; existing AGs that exceed a newly-lowered cap keep their current feeders until next edit. |

The failure email intentionally uses the plain-text SMTP path (no Claude, no HTML templating) so it still delivers when Claude itself is the reason for the failure.

**Per-AG admin override (Wave 5, 2026-04-26)** - every alert group YAML now carries an optional `admin_error_email` field, edited on the Alert Groups → Edit form. Recipient priority for failure notices:

1. The AG's `admin_error_email`, if set
2. The global `alert_group_failure_email_to` setting
3. `smtp_from`
4. `smtp_user`

Use the per-AG override in production: the customer-facing `email_address` (often a paid mailing list) must never receive failure / diagnostic notices. The router enforces this - pinned by `tests/test_wave5_admin_error_email.py::TestAGFailureRouting::test_per_ag_admin_email_wins_over_global`. See [07_email_setup.md § Splitting customer recipients from admin error notices](07_email_setup.md#splitting-customer-recipients-from-admin-error-notices-wave-5-2026-04-26).

---

## Production Hardening

Nine guards keep an alert group healthy under adversarial conditions.

### 1. Feeder freshness

Before calling Claude, the dispatcher checks each feeder's most-recent cached result age (`saved_search_history.db` → parquet mtime). A feeder whose data is older than the threshold is flagged.

| Setting | Default | Notes |
|---|---|---|
| `alert_group_max_feeder_staleness_hours` (global) | 48 | Fallback threshold for any AG. |
| `max_feeder_staleness_hours` (per-AG YAML) | - | Overrides the global. |
| `alert_group_fail_on_stale_feeder` (global) | `false` | When `true`, stale data aborts the dispatch with a failure email. |
| `fail_on_stale_feeder` (per-AG YAML) | - | Overrides the global. |

Default behaviour is **warn + annotate**: the Claude prompt is prepended with a `⚠️ STALENESS WARNING` banner listing which feeders are stale so the analysis accounts for data age. Set either `fail_on_stale_feeder` knob to `true` for strict behaviour.

### 2. Per-AG cost budget

Each alert group can cap its own Claude spending independently of the global daily budget.

| Field (AG YAML) | Purpose |
|---|---|
| `max_cost_usd_per_run` | Max estimated cost for a single dispatch. |
| `max_cost_usd_per_day` | 24-hour rolling sum across this AG (reads `claude_api_history.sqlite` filtered by `group_name`). |

Both are optional. When set, pre-flight cost estimate blocks the dispatch with a specific error message if either cap would be exceeded.

### 3. Concurrency guard

Every registered AG cron job uses `max_instances=1`, `misfire_grace_time=600`, `coalesce=True`. A slow-running dispatch cannot overlap with its own next scheduled run.

### 4. Circuit breaker (half-open since 2026-08-04)

After N consecutive error-status dispatches, the AG's `circuit_breaker_tripped` field is auto-set to `true` and `circuit_breaker_tripped_at` records the trip time.

A tripped breaker no longer blocks forever. While the cooldown window is running, scheduled dispatches skip CLEANLY: status `skipped`, no failure email (the trip itself already sent one), no error-streak growth. Once the cooldown elapses, the next dispatch runs as a **half-open probe**: the trip timestamp is refreshed at probe start (a failing probe waits a full cooldown before the next attempt) and a successful run closes the breaker automatically. Pre-2026-08-04 behaviour (skip forever plus a failure email every day until a manual reset) turned one bad week into a silent outage.

| Setting | Default | Notes |
|---|---|---|
| `alert_group_circuit_breaker_consecutive_failures` | 5 | Threshold. |
| `alert_group_circuit_breaker_auto_disable` | `true` | Master switch. |
| `alert_group_circuit_breaker_cooldown_hours` | 20 | Wait between half-open probes. 20h means a daily AG probes once per scheduled fire. |

Manual reset still works (and always wins):

```bash
curl -X POST http://localhost:5111/api/alert-groups/<name>/reset-circuit-breaker
```

A successful dispatch clears the streak and closes the breaker automatically. `force=true` bypasses the breaker entirely for a one-off manual run.

### 4b. Graduated LLM retry + salvage email (2026-08-04)

Local (router) LLM calls in AG dispatch retry transient failures with graduated backoff before failing the run: connection errors, timeouts, HTTP 429 and HTTP 5xx retry; config errors (HTTP 401/400/404, missing credential/endpoint) fail immediately. An empty-text response (the reasoning-trace starvation mode) also retries - local calls are $0 so retries cost only wall-clock. The Claude path keeps its own retry logic inside `analyzers/claude_client.py`.

| Setting | Default | Notes |
|---|---|---|
| `local_llm_retry_attempts` | 3 | Total attempts per dispatch. |
| `local_llm_retry_base_delay_seconds` | 30 | Backoff base; triples per retry, capped at 10 min (30s, 90s, 270s...). |
| `alert_group_llm_failure_prompt_fallback` | `true` | Salvage email switch. |

When the LLM call still fails after every retry, the dispatcher emails the **fully built prompt** to the AG's normal recipient (subject prefix `[SpeakesQuery SALVAGE]`) so the day's feeder data is not lost - paste it into Claude.ai or any LLM to finish the analysis manually. The run still records `status=error`, so failure telemetry and the circuit breaker are unaffected.

### 4c. Daily rate-limit grace window (2026-08-04)

Run rows are stamped at COMPLETION, so a strict rolling-24h `max_dispatches_per_day` window rejected the next day's cron fire whenever yesterday's run took more than a few seconds (observed in production: three daily AGs alternating success / rate_limited every other day). The window now shrinks by `alert_group_daily_window_grace_minutes` (default 90) to tolerate cron jitter plus run duration. A genuine second dispatch in the same day is still blocked.

### 5. Metrics endpoint

```bash
curl "http://localhost:5111/api/alert-groups/<name>/metrics?hours=24"
```

Returns total/success/error/skipped counts, success_rate, total/avg/max cost USD, total/avg tokens, consecutive_errors streak, and Claude-history-derived call count + cost over the window. See [API reference](10_api_reference.md).

### 6. Manual retry

The Last Run pill on the alert groups page is clickable - it shows the last ten attempts and prompts to re-run. Phrased as a retry when the last run was an error.

### 7. Per-AG email template override

Optional `email_template_override` field on the AG YAML supplies raw HTML. When set, `build_html_email` uses it verbatim with token substitution:

- `{{group_name}}`, `{{body_html}}`, `{{body_text}}`, `{{meta_bar}}`
- `{{searches_used}}`, `{{estimated_tokens}}`, `{{actual_tokens}}`, `{{cost_usd}}`

Empty / absent field uses the default branded template.

### 8. Dead-feeder detection

The Feeder Health panel now surfaces `last_search_run_age_hours` per feeder. A feeder whose saved-search hasn't actually executed within the staleness threshold is flagged `is_dead_feeder: true` and the health message includes the age - even when the parquet directory is populated with old data.

### 9. Complete exception coverage

Every callback and dispatcher exit path is wrapped in defensive try/except. Uncaught exceptions still emit an `alert_groups/*.parquet` log row, an `alert_group_runs.sqlite` audit row, and a failure email.

---

## Pick Capture & Backtesting

Added 2026-04-21 for the Daily Opportunity Brief. Any alert group whose prompt follows the contract below can capture its picks as structured Parquet rows for backtesting, alerting, and dedup against prior days.

### How it works

1. The AG's prompt instructs Claude to append a fenced ```json``` block to the end of every response, containing one object per opportunity surfaced. Each object carries a strict schema: `idea_id`, `instrument_type`, `instrument_id`, `direction`, `conviction_pct`, `expected_return_pct`, `position_size_tier`, `entry_price`, `suggested_buy_epoch`, `suggested_sell_epoch`, `hold_hours`, `take_profit_price`, `stop_loss_price`, `exit_catalyst`, `thesis`, `source_signals`.
2. After `call_messages_create` returns, the dispatcher calls `_extract_and_log_picks()` which:
   - Regex-matches the trailing ```json [...] ``` block.
   - Parses the JSON array. Parsing is lenient toward the two malformations LLMs actually produce - a missing comma between members and a trailing comma before a closing bracket (added 2026-07-10 after the local 122B dropped one comma and six valid picks went unjournaled). The repair is error-position-driven (the decoder says exactly where it choked; only that character is touched), a warning logs each auto-repair, and anything else still fails the block cleanly. The same leniency covers the review-observations and playlist-composer JSON tails.
   - Validates each object against the schema (required keys, known enum values, positive epochs, `sell >= buy`).
   - Lowercases `idea_id` / `instrument_type` / `instrument_id` as the "verify" step on top of trusting Claude.
   - Writes one row per valid pick via `log_ag_pick()` → `indexes/IMMUTABLE/ag_picks/*.parquet`.
3. Parse / validation failures log a warning and continue - the brief email still ships even if capture fails, and individual bad picks are skipped rather than poisoning the batch.

### Dedup / throttle loop

A feeder `dob_reserved_picks` (shipped as a default saved search) queries the last 24 hours of `ag_picks` and renders the idea_ids + ranks + theses as the 11th data block in the next dispatch's prompt. Claude is instructed to treat these as **reserved** - not to re-suggest unless a material new catalyst has emerged. On day 1 the feeder is empty (SPQL engine's empty-DataFrame short-circuit handles that gracefully); from day 2 onward Claude starts seeing its prior picks.

The 24h window is a fixed constant today. Switching to a horizon-aware window (idea reserved until its `suggested_sell_epoch` has passed) is a one-line query edit when you're ready.

### Querying captured picks

Like any other `indexes/` Parquet, `ag_picks` is SPQL-native. Example queries you'll find yourself writing:

```spql
# Every pick from the last 7 days, highest conviction first
index="indexes/IMMUTABLE/ag_picks/*.parquet"
  | where _epoch >= now() - 604800
  | sort -conviction_pct, -_epoch
  | table event_timestamp, idea_id, direction, conviction_pct,
          expected_return_pct, entry_price, hold_hours, thesis
```

```spql
# Distinct idea_ids suggested today
index="indexes/IMMUTABLE/ag_picks/*.parquet"
  | where _epoch >= now() - 86400
  | dedup idea_id
  | table idea_id, direction, conviction_pct, exit_catalyst
```

```spql
# Open picks whose planned exit is in the next 6 hours (cue to check status)
index="indexes/IMMUTABLE/ag_picks/*.parquet"
  | where status="open"
     AND suggested_sell_epoch >= now()
     AND suggested_sell_epoch < now() + 21600
  | sort suggested_sell_epoch
```

### Backtesting - building the resolution loop

The schema is deliberately backtest-ready. A future ingestion script (sandboxed or `_pro`) can:

1. Read `indexes/IMMUTABLE/ag_picks/` for rows with `status="open"`.
2. For each pick, fetch current price from the appropriate API (CoinGecko for crypto, Polymarket gamma-api for prediction markets, Yahoo for equities, etc. - all already on the `allowed_api_domains` allowlist).
3. Compute verdict:
   - If `max(price)` in `[suggested_buy_epoch, suggested_sell_epoch]` ≥ `take_profit_price` → `status="won"`, magnitude = `take_profit_price − entry_price`.
   - Else if `min(price)` in window ≤ `stop_loss_price` → `status="lost"`, magnitude = `entry_price − stop_loss_price`.
   - Else (time elapsed, no threshold hit) → `status="time_exit"`, magnitude = `price(suggested_sell_epoch) − entry_price`.
4. Append-only emit to `indexes/logs/ag_pick_resolutions/` (separate category; keeps the capture log immutable). Or update-in-place with the `status` field rewrite.

Once the resolutions index exists you can wire alerts on it: a saved search that emails you on resolution with the verdict and magnitude. No new infrastructure - same pattern as every other alert.

The backtest script is not shipped with this change (deliberately scope-limited). When you want it, the capture schema has everything it needs.

### Adding pick capture to a different alert group

Three steps:

1. Add a mandatory `MANDATORY STRUCTURED TAIL` section to the AG's `prompt_text` with the exact JSON schema (copy from `alert_groups/daily_opportunity_brief.yaml`).
2. If you want dedup on that AG, create a feeder querying `indexes/IMMUTABLE/ag_picks/*.parquet | where alert_group="<your_ag_name>" | ...` and add it to `search_names`.
3. The dispatcher's `_extract_and_log_picks()` is generic - no code change needed. Every AG calling through the standard dispatcher gets capture for free if its prompt includes the tail.

### Wave 3 (2026-04-25): Manual return loop for prompt-only deliveries

Alert groups with `delivery_mode: prompt_only` email the operator the prompt instead of calling the Claude API. The operator pastes that prompt into Claude.ai / ChatGPT / Gemini / etc., gets back a brief, and historically had no way to get those picks into `ag_picks` short of running the full (paid) Claude pipeline.

The **Upload Brief** button on every Alert Groups row (Wave 3) closes that loop. Behind the modal:

- **Backend:** `POST /api/alert-groups/<name>/manual-return` accepts `{raw_text, model_used, dispatch_run_id?, dry_run?}`. Reuses the same `_parse_picks_block` parser the live dispatcher uses (refactored for purity), so a paste from any LLM whose output follows the mandatory-tail contract just works.
- **Provenance:** every captured row carries `source` (`"claude"` for live-dispatch picks, `"manual"` for operator pastes) and `model_used` (e.g. `"gpt-4o"`, `"claude-sonnet-4-6"`, `"gemini-2.5-pro"`). Old rows read NULL for both - the schema addition is non-breaking.
- **Dispatch linkage:** if the operator passes `dispatch_run_id` (the original Claude `request_id` from the prompt-only email's banner), the manual return joins cleanly to the same dispatch. Otherwise a synthetic `manual:<group>:<UTC>` id is generated.
- **Dedup:** SHA-256(`alert_group + raw_text`); identical pastes within the last 7 days return HTTP 409 with the prior `run_request_id`. Operator can re-edit the text or just trust the prior write.
- **Preview pane:** **Preview parsed picks** posts with `dry_run=true` so the operator sees exactly what would land before committing. Submit posts with `dry_run=false`.
- **Back-fill friendly:** the endpoint accepts any `dispatch_run_id` (or none), so operators can paste a brief days late for a dispatch they missed at the time.

Once captured, manual returns are queryable through the same `ag_picks` index as Claude-pipeline picks, with `source="manual"` for filtering:

```spql
# Compare model performance over the last 30 days across all sources
index="indexes/IMMUTABLE/ag_picks/*.parquet"
  | where _epoch >= now() - 2592000
  | stats count by model_used, source
  | sort -count
```

```spql
# Just operator-pasted picks for one AG
index="indexes/IMMUTABLE/ag_picks/*.parquet"
  | where alert_group="daily_opportunity_brief"
  | where source="manual"
  | sort -_epoch
  | table _epoch, model_used, idea_id, conviction_pct, thesis
```

---

## API Endpoints

All endpoints follow the standard SpeakesQuery response envelope (`{"status": "success", ...}` or `{"status": "error", "message": "..."}`).

### Boilerplate Prompts

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/boilerplate-prompts/list` | List all prompts |
| `POST` | `/api/boilerplate-prompts/create` | Create a prompt |
| `GET` | `/api/boilerplate-prompts/<name>` | Get a prompt by name |
| `PUT` | `/api/boilerplate-prompts/<name>` | Update a prompt |
| `DELETE` | `/api/boilerplate-prompts/<name>` | Soft-delete a prompt |
| `GET` | `/api/boilerplate-prompts/<name>/yaml` | Get raw YAML |

### Alert Groups

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/alert-groups/list` | List all groups |
| `POST` | `/api/alert-groups/create` | Create a group |
| `GET` | `/api/alert-groups/<name>` | Get a group by name |
| `PUT` | `/api/alert-groups/<name>` | Update a group |
| `DELETE` | `/api/alert-groups/<name>` | Soft-delete a group |
| `GET` | `/api/alert-groups/<name>/yaml` | Get raw YAML |
| `POST` | `/api/alert-groups/<name>/run` | Manually trigger a dispatch |
| `POST` | `/api/alert-groups/<name>/enable` | Enable a group |
| `POST` | `/api/alert-groups/<name>/disable` | Disable a group |
| `GET` | `/api/alert-groups/runs` | List run history (`?group_name=...&limit=N`) |
| `GET` | `/api/alert-groups/<name>/feeder-status` | Report per-feeder deployment + credential + data health (see [Feeder Health](#feeder-health) below) |
| `POST` | `/api/alert-groups/<name>/deploy-feeders` | Bulk-install missing default feeders **and** deploy every library script referenced by this group's feeders that isn't already scheduled |
| `POST` | `/api/alert-groups/<name>/install-default-feeder/<search_name>` | Install a single project-shipped default feeder (copies `default_saved_searches/<search_name>.yaml` into `saved_searches/`). Query param: `?overwrite=true` force-replaces an already-installed YAML with the current template (for syncing stale Docker-volume copies to the latest template after a bug-fix rebuild). The response JSON returns `resynced: true` when overwrite took effect. |
| `GET` | `/api/alert-groups/<name>/pipeline-health` | Deep health check - feeder-status **plus** per-feeder SPQL execution; reports `query_row_count`, `query_error`, `query_columns`, `fresh_row_count` |
| `POST` | `/api/alert-groups/<name>/run?dry_run=true` | Build the Claude prompt payload without calling Claude or sending email; returns the exact messages that would be sent |

---

## Feeder Health

Alert groups depend on a chain of moving parts: the saved searches they reference, the Parquet indexes those searches query, the ingestion scripts that produce those indexes, and any credentials those scripts require. When any link is missing, the group silently produces empty analysis - which is hard to diagnose from the run history alone.

The **Feeder Health** panel surfaces every link's state at a glance.

### UI

The Alert Groups page shows a **Health** pill on every row with an aggregate verdict (e.g. `✓ 7/10 live`, `⚠ Needs deploy (2/10 live)`). Click it to open the Feeder Health modal, which lists each feeder saved search with:

- A colour-coded state pill (see states below).
- The library script that would produce its index, if resolvable.
- The deployed ingestion task id + file count, if any.
- An inline action button where appropriate (Set credentials, Deploy, etc.).

If any feeder is in the `needs_deploy` state, the modal shows a **Deploy Missing Feeders** button that batch-creates all outstanding ingestion tasks in one click. Credentials the user pre-staged (on `script_id=0`) automatically migrate to the new tasks via `engine.migrate_staging_credentials`.

### Feeder states

| State | Meaning |
|-------|---------|
| `live` | Parquet files exist under the expected index directory. Data is flowing. |
| `pending` | Ingestion task is deployed + enabled + credentialed, but no data has landed yet. Give it a cron cycle. |
| `disabled` | Ingestion task exists but is disabled. Enable it in **Ingestion Scripts**. |
| `needs_creds` | Ingestion task is deployed but the library script's required credentials aren't in the vault. Set them on the task. |
| `needs_deploy` | A library script matches this feeder's index, but isn't scheduled. Click **Fix Missing Feeders**. |
| `no_library_script` | No curated library script matches this index - likely a user-managed/custom ingestion pipeline. If the index has data, the state flips to `live` with an informational note. |
| `missing_search` | The alert group references a saved search name that doesn't exist. If the resolver also set `installable: true`, a project-shipped default template is available - click **Install default** (or use the bulk Fix button). Otherwise you'll need to create the search yourself. |
| `unknown_index` | The saved search has no `index="…"` clause - can't resolve what to check. |

### Default feeder templates

The project ships the feeders for every default alert group under `default_saved_searches/` (tracked in git). This directory is the source of truth for templates; user-editable copies live under `saved_searches/` (gitignored, so user edits and personal searches don't leak into commits).

- **Auto-seed on first run**: when `SavedSearchStore.initialize()` runs, any `default_saved_searches/*.yaml` whose filename doesn't already exist under `saved_searches/` is copied across. Seeding is idempotent - user edits and deletions are respected (a re-seed will not overwrite a user-modified file, only fill in missing ones).
- **On-demand install**: if a feeder was deleted or never seeded, the Feeder Health modal shows an **Install default** button per missing-but-installable feeder. The bulk **Fix Missing Feeders** button installs every installable default *and* deploys every needed library script in one call.
- **Resolver flag**: `missing_search` feeders carry an `installable: true` field when a matching default exists; the UI uses it to decide whether to show an Install action or a "Create this search" hint.

### Resolution chain

For every feeder, the resolver (`alert_groups/feeder_status.py`) walks:

1. Load the saved search by name → extract the first `index="…"` path from its SPQL query.
2. Normalise the path: strip `indexes/` prefix and any trailing glob/extension → canonical subdirectory (e.g. `polymarket/high_probability_pro`).
3. Find the library script whose `suggested_subdirectory` matches.
4. Find the scheduled ingestion task whose `subdirectory` matches.
5. If the task exists, check the credential vault for every key in `requires_credentials`.
6. Count parquet files under `indexes/<subdirectory>/` for the data-freshness signal.

Resolver inputs are injected (not imported), so the module is a pure function tree - unit-tested end-to-end in `TestFeederStatusResolver`.

### API - feeder-status response

```json
{
  "status": "success",
  "group_name": "daily_opportunity_brief",
  "summary": {
    "counts": {"live": 6, "needs_deploy": 3, "needs_creds": 1, "pending": 0, ...},
    "overall": "needs_deploy",
    "total": 10
  },
  "feeders": [
    {
      "search_name": "dob_poly_high_prob",
      "state": "live",
      "index_paths": ["indexes/polymarket/high_probability_pro/*.parquet"],
      "subdirectory": "polymarket/high_probability_pro",
      "library_script_id": "polymarket_high_probability_pro",
      "task_id": 17,
      "task_enabled": true,
      "required_credentials": [],
      "missing_credentials": [],
      "data_file_count": 42,
      "last_data_epoch": 1723848240.0,
      "message": "42 parquet file(s) present under indexes/polymarket/high_probability_pro."
    }
  ]
}
```

The `summary.overall` field is the **worst** state across all feeders (ranked `live` < `pending` < `disabled` < `needs_creds` < `needs_deploy` < `no_library_script` < `missing_search` < `unknown_index`), so the UI can show a single at-a-glance verdict.

### API - deploy-feeders response

By default the endpoint also chains a synchronous `run_task_now` against every newly-deployed task **and** every existing `pending` task (deployed earlier but no parquet yet) so the operator gets immediate feedback. Pass `?run_after_deploy=false` to keep the deploy-only behaviour. `?max_run_workers=N` (1-8, default 4) caps parallelism for the run-now phase.

```json
{
  "status": "success",
  "group_name": "daily_opportunity_brief",
  "ran_after_deploy": true,
  "installed": [
    {"search_name": "dob_poly_high_prob", "source": "default_saved_searches"}
  ],
  "deployed": [
    {"search_name": "dob_sec_catalysts", "library_script_id": "sec_major_filings_feed",
     "task_id": 25, "subdirectory": "sec/major_filings",
     "cron_schedule": "0 5,11 * * *",
     "cron_source": "ag_schedule_minus_60min",
     "ag_schedule": "0 6,12 * * *",
     "requires_credentials": ["SEC_EDGAR_CONTACT"]}
  ],
  "skipped": [
    {"search_name": "dob_poly_high_prob", "reason": "live", "library_script_id": "polymarket_high_probability_pro"}
  ],
  "failed": [],
  "runs": [
    {"search_name": "dob_sec_catalysts", "task_id": 25,
     "trigger_reason": "newly_deployed", "skipped": false,
     "run": {"status": "success", "rows_inserted": 187, "runtime": 2.41,
             "error_message": null}}
  ],
  "feeder_status": { /* same shape as /feeder-status, post-deploy + post-run */ }
}
```

Feeders with state other than `needs_deploy` are recorded under `skipped` with their current state as the reason - the endpoint is idempotent and safe to re-invoke.

**Why the chain runs by default** - before this change, "Fix Missing Feeders" deployed scripts but left them sitting until the next cron tick. Operators then ran Pipeline Check, saw 0 rows for every feeder, and assumed the AG was broken. Chaining run-now closes that loop: ingestion happens immediately, the modal renders rows-inserted per feeder, and Pipeline Check auto-refreshes so the operator sees the post-run SPQL output without an extra click.

### Ingestion cron alignment

When the alert group has a schedule, newly-deployed ingestion tasks are **cron-aligned to fire 60 minutes before each AG dispatch** rather than running on the library script's default cadence. The goal is that Parquet data is fresh by the time the alert group queries it (the saved-search cache sits between the two, typically firing 30 minutes before the AG).

The derivation is algebraic and only handles simple expressions - literal minutes + literal hour lists. If the AG cron uses steps (`*/30`), ranges (`6-8`), a wildcard hour, or would cross midnight when shifted, the deploy falls back to the library script's `suggested_cron`. The `cron_source` field on each `deployed` entry reports which path was taken:

| `cron_source` | Meaning |
|---------------|---------|
| `ag_schedule_minus_60min` | Derived from AG schedule, 60 min earlier. Data-fresh guarantee. |
| `library_suggested` | Fell back to the library script's `suggested_cron` (e.g. `*/30 * * * *` continuous polling). |
| `engine_default` | Library script had no `suggested_cron` - deployed with the engine's `*/30 * * * *` safety default. |

The logic lives in `alert_groups/feeder_status.py::derive_pre_cron` - pure function, fully unit-tested (10 edge cases including midnight, weekday-only, malformed, wildcard, range).

Example for `daily_opportunity_brief` with schedule `0 6,12 * * *`:
- AG fires at 06:00 and 12:00
- Saved searches fire at 05:30 and 11:30 (30 min before AG - set in the feeder template)
- Ingestion scripts fire at **05:00 and 11:00** (60 min before AG - set by this endpoint)

---

## End-to-End Pipeline Validation

Two endpoints + a UI workflow let you validate every link in the ingestion → dispatch chain without spending Claude credits or sending test emails.

### Deep pipeline-health check

`GET /api/alert-groups/<name>/pipeline-health` extends the basic feeder-status with actual SPQL execution. For each feeder whose state isn't `missing_search` / `needs_deploy` / `unknown_index`, it:

1. Loads the saved search YAML.
2. Runs the SPQL against the live indexes via the real query backend.
3. Records `query_row_count`, `query_error` (if any), `query_columns`, and `fresh_row_count` (rows within a 24-hour dispatch window).

If the SPQL raises at runtime (the canonical case: an upstream API format change breaks a `strptime` call in the query), the feeder's state flips to `query_broken` - a state invisible to the basic feeder-status endpoint because Parquet data still lands.

**UI:** Click **Run Pipeline Check** in the Feeder Health modal. Each feeder row gets an extra line: `SPQL returned N row(s) (M within dispatch window)` in green, or `Query error: …` in red.

**Zero-row classification (Wave 2, 2026-04-25)** - feeders that returned 0 rows get a tag + actions distinguishing two distinct failure modes:

| Tag | When | Available action |
|---|---|---|
| **Likely sparse** (yellow) | `data_file_count > 0` AND `query_row_count == 0` - parquet has rows but the saved-search query filtered to zero. Common on quiet days. | **Go to ingestion task →** (the search filter is the suspect, not the ingestion). |
| **Likely broken** (red) | `data_file_count == 0` - no parquet under the feeder's subdirectory. Ingestion never produced output. | **Run ingestion now** (synchronous via `POST /api/si/<task_id>/run`) + **Go to ingestion task →**. The Run-now button auto-re-runs Pipeline Check on success so the row-count climbs from 0 → N inline. |

**Go to ingestion task →** switches to the Ingestion Scripts tab and scroll-highlights the matching task row. The cross-tab nav targets `tr[data-si-task-id="<id>"]` - pinned by `tests/test_alert_group_deploy_run_chain.py::TestNavigationContract`.

### Dispatch dry-run

`POST /api/alert-groups/<name>/run?dry_run=true` runs everything through the messages-build step, then **stops**. No Claude call. No email. Response carries the full message payload that would have been sent:

```json
{
  "status": "success",
  "dry_run": true,
  "run": {
    "status": "dry_run",
    "searches_used": ["dob_poly_high_prob", "dob_kalshi_poly_arb", "…"],
    "estimated_tokens": 18472,
    "actual_tokens": 0,
    "cost_usd": 0.0
  },
  "preview": {
    "messages": [{"role": "user", "content": "<prompt + blocks>"}],
    "searches_used": [...],
    "estimated_tokens": 18472
  }
}
```

Use this to sanity-check the prompt before committing to real cost - confirm every feeder contributed rows, the prompt text renders correctly, and the estimated token count is within budget.

**UI:** Click **Preview Dispatch Prompt** in the Feeder Health modal. A modal opens with the full prompt + data blocks rendered as text, plus a meta line showing the searches included and the estimated token count.

### End-to-end pipeline tests

`tests/test_daily_brief_pipeline.py` parametrizes over all 10 feeders of `daily_opportunity_brief` and asserts the full chain `library script + mocked HTTP → DataFrame → tmp Parquet → feeder SPQL → result rows`. This catches:

- Script execution failures (bad imports, missing columns, sandbox violations)
- Column contract drift (saved search projects columns the script doesn't produce)
- SPQL parse or execution errors against real parquet data
- Regression in any of the above on both PR-time and scheduled CI

Each feeder is a ~10s test; full suite runs in ~90s (network-free, deterministic, uses the same mock routers as `test_script_library.py`).

---

## Troubleshooting

**"No cached result found for search X"** - The referenced saved search has never executed, or its Parquet result file has been deleted. Run the saved search at least once before including it in an alert group.

**"No prompt text configured"** - The alert group's `prompt_text` field is empty. Add your instructions via the UI or API.

**"No Claude API key configured"** - Store your Anthropic API key in the credential vault via Settings → Claude Analyzer → API Key, or via `POST /api/settings/analyzer-key`.

**"All results trimmed - nothing to send"** - The token budget gate is too low for the combined search results. Increase `claude_analyzer_daily_budget_cents` in Settings or reduce `max_rows` on the alert group.

**"Group is disabled"** - Scheduled run was skipped because `disabled` is true. Enable via `POST /api/alert-groups/<name>/enable` or click the status toggle in the UI.

**Empty `email_address`** - The dispatch runs and calls Claude, but no email is sent. This is useful for testing via the manual run endpoint, which returns the response text directly.

**UI stuck on "Dispatching to Claude..." for minutes** - This is usually **expected behaviour**, not a bug. A web_search-enabled analyst brief with 10 feeders and `max_output_tokens=16384` legitimately takes 2–10 minutes: the Claude call alone can use 3+ minutes of server-side thinking time while it dispatches web_search tool calls. To distinguish normal work from a true hang, tail the docker logs:

```bash
docker logs -f <container-name>
```

The dispatcher emits one `[i]` log line at every phase boundary (2026-04-21 onward):

```
[i] AG '<name>': feeder loop start (10 feeders)
[i] AG '<name>': feeder [1/10] 'dob_poly_high_prob' running...
[i] AG '<name>': feeder 'dob_poly_high_prob' executed on-demand (50 rows, 312ms)
...
[i] AG '<name>': feeder loop done (10/10 feeders produced data, 580 rows total, 4127ms)
[i] AG '<name>': calling Claude (model=claude-sonnet-4-6, max_tokens=16384, est_input_tokens=18442, timeout=600s, retry_attempts=3, tools=web_search)
[i] AG '<name>': Claude returned (in=18440, out=6120, stop=end_turn, cost=$0.1470, latency=187543ms, attempts=1)
[i] AG '<name>': sending email to ops@example.com
[i] AG '<name>': email sent (412ms)
[i] AG '<name>': dispatch complete (10 searches, 18442 est. tokens, total 192082ms).
```

If you see `calling Claude` but no `Claude returned` line within the configured `claude_request_timeout_seconds` (default `600s = 10min`), the dispatch is genuinely wedged - at that point it's a Claude API or network issue, and the wrapper will raise and the dispatcher will log `Claude API error after <ms>:` with the class of error. **`APITimeoutError` is NOT retried** (retrying a timeout just hits the same wall), so a timeout fails once after 600s rather than after `600 × 4 = 2400s = 40min`. Connection and 429/5xx errors still retry up to `claude_retry_attempts + 1` times with exponential backoff. The failure email (if `alert_group_failure_email_enabled`) fires for terminal errors. If you see `feeder loop done` but no `calling Claude` line for many seconds, something between the serializer and the Claude wrapper is wedged (exceedingly rare - check the per-AG budget gate).

**`[x] Error starting JVM: No JVM shared library file (libjvm.so) found`** - You're on a Docker image predating 2026-04-21. Rebuild with `./update.sh`: the `jpype1` dependency and its dead JVM coercion path were removed, so the log spam disappears. If it persists on a fresh image, you've resurrected a `jpype` import - run `pytest tests/test_no_jpype_and_dispatch_logging.py::TestNoJpype` to find out where.

**Claude call timed out after 120s, retry loop burned 8 minutes, no brief delivered** - You're on a Docker image predating 2026-04-21 04:00 UTC. Rebuild with `./update.sh`: the default `claude_request_timeout_seconds` was raised from 120 → 600, and `APITimeoutError` was removed from the retry-classifier (retrying a timeout just hits the same wall). If the error surfaces even on the new image, a specific AG needs a longer timeout - adjust via Settings → Claude Analyzer → Request timeout (ceiling 3600s for a 30-feeder heavyweight brief).

**Feeder dropped out with "No cached result found for search X" but the feeder clearly exists** - The live on-demand query errored and the dispatcher fell back to the saved-search cache, which also missed. Look one log line above: as of 2026-04-21 the dispatcher uses `process_query_with_diagnostics()` (in `query_engine/CmdExecutionBackend.py`) which propagates the actual error class + message, and logs it with the feeder name (e.g. `[!] AG 'X': feeder 'Y' error after Nms - UndefinedVariableError: divergence_pct not defined`). The generic "No cached result" warning is now only emitted when BOTH the live execution AND the cache legitimately miss. Common causes: installed saved-search YAML is stale (see below), ingestion script produced empty output without schema preservation, or a column referenced in `| sort` was dropped by a prior `| table`.

**Feeder Health shows a yellow "Sync Template" badge** - Your installed `saved_searches/<name>.yaml` has drifted from the git-tracked `default_saved_searches/<name>.yaml` template. The Docker volume was seeded before a bug-fix commit. Click **Sync Template** in the Feeder Health modal to overwrite the installed YAML with the current template - confirms with a dialog since it clobbers any manual edits. Programmatic equivalent: `POST /api/alert-groups/<ag>/install-default-feeder/<search>?overwrite=true`.

**Feeder query fails with `name 'X' is not defined` where X is a real column** - The source Parquet has zero rows AND zero columns. As of 2026-04-21, the SPQL engine short-circuits `where`/`table`/`sort` on empty input, and kalshi-style scripts emit the schema even on empty days. If you still see this, check the underlying ingestion script - it may need `pd.DataFrame(rows, columns=EXPECTED_COLUMNS)` so the empty-day Parquet carries the schema. Pattern reference: `script_library/scripts/kalshi_polymarket_arbitrage_pro.json`.

**Scheduled run appeared not to fire but the AG has a `max_dispatches_per_day` or `min_interval_between_runs_hours`** - Check the runs table: `curl http://localhost:5111/api/alert-groups/runs?group_name=<name>&limit=5`. If you see a row with `status="rate_limited"` at the expected fire time, the cron DID fire - the dispatcher blocked it because a prior successful run (often a recent manual test) consumed today's quota. This is working as designed. Two ways to override for a single dispatch:

```bash
# Manual force-run (bypasses rate limit + circuit breaker; budget + freshness still apply)
curl -X POST "http://localhost:5111/api/alert-groups/<name>/run?force=true"
```

Or edit the AG in the UI → Advanced section → adjust the limit. Caught 2026-04-21 when a user ran the Daily Brief manually at 03:31 UTC (consuming the 20h slot) and then the 11:30 UTC scheduled cron status=rate_limited. The manual-run UI offers an inline "force-run" prompt when rate-limited; scheduled runs just log the status.

**Settings UI "Claude API History" section empty even though dispatches are succeeding** - The Parquet log (`indexes/logs/claude_api/*.parquet`) and the SQLite forensic audit (`claude_api_history.sqlite`) are two different surfaces. The Settings UI page reads from the SQLite. If the SQLite is empty but Parquet has rows, the SQLite is being wiped - most commonly because the file is missing from the Docker bind-mount list and gets re-created (ephemeral) on every restart.

Fixed 2026-04-21: `claude_api_history.sqlite` + `analyzer_results.sqlite` are now in both `install.sh` touch list and `desktop_app/docker-compose.yml` volumes. If you're on an older image:

```bash
cd ~/speakesQuery          # or wherever the host checkout lives
touch claude_api_history.sqlite analyzer_results.sqlite
./update.sh
```

A startup sanity check now raises a loud ``RuntimeError`` at first API use if the sqlite path is a directory (the fingerprint of Docker having auto-created the bind-mount target), with the exact remediation steps in the message. Pinned by `tests/test_docker_sqlite_mounts.py`.

---

## Reference: Daily Opportunity Brief

A complete worked example ships with the repository. It is a production-shaped alert group designed to identify the top 5 daily investment opportunities across prediction markets, crypto, equities, options, and government contracts, with a strict 8-hour decision runway and 75%+ conviction bar.

**Files shipped:**

- `alert_groups/daily_opportunity_brief.yaml` - the alert group config
- `boilerplate_prompts/daily_opportunity_brief.yaml` - the reusable prompt template
- `saved_searches/ag_*.yaml` - ten row-capped SPQL searches feeding the group
- `script_library/scripts/earnings_calendar_72h.json` - Nasdaq earnings calendar scraper
- `script_library/scripts/options_unusual_activity_pro.json` - Finnhub options chain volume/OI anomaly detector (greeks + bid/ask from the API)

**Scheduling:**

| Component | Cron | Purpose |
|---|---|---|
| Ingestion scripts (8 existing + 2 new) | each script's `suggested_cron` | Write Parquet into `indexes/...` on a rolling basis |
| 10 saved searches (`ag_*`) | `30 5,11 * * *` | Cache row-capped SPQL results 30 min before each dispatch |
| Alert group (`daily_opportunity_brief`) | `0 6,12 * * *` | Morning brief at 06:00 local, midday re-check at 12:00 local |

**Signal streams (10):**

1. `dob_poly_high_prob` - Polymarket markets priced 75-95% with expiry > 24h
2. `dob_kalshi_poly_arb` - Kalshi / Polymarket cross-platform arbitrage (≥5% divergence)
3. `dob_poly_volume_spikes` - Polymarket volume anomalies in the 0.30-0.70 edge zone
4. `dob_crypto_anomalies` - CoinGecko top-200 coins with HIGH/CRITICAL volume-mcap ratio
5. `dob_sec_catalysts` - SEC Form 4 insider trades + 8-K material events (top 15 companies)
6. `dob_reddit_buzz` - Tickers with HIGH/VIRAL buzz across ≥2 financial subreddits
7. `dob_gov_contracts` - MEGA (>$100M) and VERY_LARGE (>$50M) federal contract awards
8. `dob_macro_regime` - FRED fear gauges (VIX, HY spread, stress index) for regime context
9. `dob_earnings_72h` - Upcoming earnings 8-72 hours out (NEW - Nasdaq scraper)
10. `dob_options_unusual` - Unusual options vol/OI ratios on 15 liquid tickers (Finnhub, requires `FINNHUB_API_KEY` in Global Credentials - free signup at finnhub.io, 60 calls/min)

**Required setup:**

1. Raise the Claude budget gate from 50¢/day to ~200¢/day via Settings → Claude Analyzer → Daily Budget - web search tool calls add ~$0.01 each and the analyst typically calls it 5-10 times per pick.
2. Add an Anthropic API key to the credential vault (Settings → Claude Analyzer → API Key).
3. Leave `email_address` blank on the alert group while validating - use `POST /api/alert-groups/daily_opportunity_brief/run` and inspect the response JSON inline.  Once the output shape is good, add a recipient through the UI.

**Verification path:**

```
source env/bin/activate
pytest tests/test_script_library.py -vv -k "earnings_calendar_72h or options_unusual_activity_pro"
# Start the server, then:
curl -X POST http://localhost:5111/api/alert-groups/daily_opportunity_brief/run
```

See the prompt template file for the full instruction text Claude receives, including the strict 8-hour runway rule and the required output structure.
