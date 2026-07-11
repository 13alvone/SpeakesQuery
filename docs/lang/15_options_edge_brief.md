# Options Edge Brief (OEB)

The Options Edge Brief is a twice-daily analyst brief dedicated to options trading. It surfaces 5–10 picks per dispatch across five signal classes - IV rank, term structure, skew, pre-earnings implied move, and unusual activity - and renders each pick at three difficulty tiers (BEGINNER / INTERMEDIATE / ADVANCED) so the reader can pick whichever structure they're comfortable with for the same underlying thesis.

## Audience

Designed for someone learning options trading who plans to ramp from paper-trading to a small (~$1000) live account once the brief's picks demonstrate measurable edge. Every pick:

- Defines greeks inline rather than assuming knowledge.
- Carries explicit risk-management rules (entry, stop-loss, take-profit, time stop).
- Computes the **minimum account size** the BEGINNER tier fits at ≤2% sizing on 1 contract.
- Flags picks that don't fit a $1000 account so the reader paper-trades them rather than rejecting them.

## Schedule

```
30 14,19 * * 1-5
```

Twice daily on US weekdays - **14:30 UTC** (10:30 ET, post-open vol settle) and **19:30 UTC** (15:30 ET, pre-close institutional positioning). Capped at 2 dispatches per day with a 4-hour minimum interval between successful runs.

## Signal Streams (6 feeders)

| Feeder | Source script | Edge it surfaces |
|--------|---------------|------------------|
| `oeb_iv_rank` | [options_iv_rank_screener_pro](../../script_library/scripts/options_iv_rank_screener_pro.json) | SELL_PREMIUM (IVR ≥ 70) and BUY_PREMIUM (IVR ≤ 30) - the most-used options filter |
| `oeb_term_structure` | [options_term_structure_pro](../../script_library/scripts/options_term_structure_pro.json) | BACKWARDATION (front IV > back IV) - event-premium calendar-spread setups |
| `oeb_skew_extreme` | [options_skew_monitor_pro](../../script_library/scripts/options_skew_monitor_pro.json) | STRESS_BIDDED 25-delta skew - fear gauge / contrarian long-equity setups |
| `oeb_earnings_implied_move` | [options_earnings_implied_move_pro](../../script_library/scripts/options_earnings_implied_move_pro.json) | HIGH_IV pre-earnings ATM straddles - IV-crush opportunities |
| `oeb_unusual_activity` | [options_unusual_activity_pro](../../script_library/scripts/options_unusual_activity_pro.json) | vol/OI ratio spikes - smart-money positioning footprint |
| `oeb_session_context` | [options_market_status](../../script_library/scripts/options_market_status.json) + [options_ex_div_calendar](../../script_library/scripts/options_ex_div_calendar.json) | Session state, holidays, ex-div windows - gating layer |

All five signal scripts target Massive.com's `api.massive.com` (formerly polygon.io) Options Starter tier. The user supplies `MASSIVE_API_KEY` (also accepted as `POLYGON_API_KEY` for backward compatibility) in Settings → Global Credentials.

## Three-Tier Learner Format

Every pick is rendered at three difficulty levels, all expressing the SAME directional / volatility thesis on the SAME underlying:

| Tier | Structures | Risk profile | When to use |
|------|-----------|---------------|-------------|
| 🟢 BEGINNER | Long call, long put, cash-secured put, covered call | Single-leg, defined risk both sides | New to options - learning the contract mechanics |
| 🟡 INTERMEDIATE | Bull/bear call/put debit/credit spreads | 2 legs, capped max profit AND max loss | Comfortable with the greeks at a high level |
| 🟣 ADVANCED | Iron condor, calendar, straddle, strangle | 3-4 legs, vega + theta exposure | Understand the greeks individually + comfortable monitoring multiple leg fills |

The same thesis ("NVDA earnings IV is too high") maps to:
- 🟢 sell put credit spread (defined risk, simple)
- 🟡 bear put debit spread (capital-efficient directional)
- 🟣 short straddle / iron condor (pure short-volatility)

The reader picks one tier and trades that. Only the BEGINNER tier is journaled to the pick history by default - the others appear in the markdown body for educational purposes.

## Risk-Management Rules (baked into every pick)

**Long-premium trades** (long calls, long puts, debit spreads):
- Entry: limit at mid ± 5% of the bid-ask spread
- Stop-loss: close at -50% of premium paid
- Take-profit: scale out at +100% (close half), trail remainder
- Time stop: close if 50% of DTE elapsed without thesis playing out

**Short-premium trades** (covered calls, cash-secured puts, iron condors, short straddles):
- Entry: limit at mid ± 5% of credit received
- Stop-loss: close if credit DOUBLES against you
- Take-profit: close at 50% of max credit (the TastyTrade "manage at 50%" rule)
- Time stop: ALWAYS close at 21 DTE - gamma risk spikes inside that window

## Account-Size Awareness

Every pick computes `account_size_floor_usd` - the minimum account size needed to hold 1 contract at ≤2% sizing. Examples:

- BUY NVDA $115 PUT @ $2.10 → 1 contract = $210 risk → floor = $10,500
- BUY AAPL $150 PUT @ $0.40 → 1 contract = $40 risk → floor = $2,000
- Bull put spread, $1.40 net debit → floor = $7,000

If a pick's `account_size_floor_usd` exceeds $1000, the brief inserts a "💰 Account-size note" in the markdown directing the reader to either skip the pick or substitute a lower-strike alternative. The full pick is still journaled - Wave 2 attribution will track whether the user's actual account size at the time supported the pick.

## Pick Journal Schema

Every pick lands in `indexes/IMMUTABLE/ag_picks/*.parquet` via the standard `log_ag_pick` pathway, with eight new options-specific columns added in Wave 1:

| Column | Type | Purpose |
|--------|------|---------|
| `option_structure` | str | `long_call` / `long_put` / `vertical_debit_spread` / `iron_condor` / `calendar` / `straddle` / `strangle` / `covered_call` / `cash_secured_put` |
| `option_legs_json` | str (JSON) | Array of leg objects: `{action, right, strike, expiration, qty, limit, contract_symbol}` |
| `option_max_loss_usd` | float | Max dollar risk per 1 contract (positive) |
| `option_max_profit_usd` | float \| null | Max dollar profit per 1 contract; NULL for unlimited (long calls) |
| `option_net_debit_credit` | float | Positive = net debit paid, negative = net credit received |
| `option_dte_days` | int | Days to expiration of longest-DTE leg |
| `option_difficulty_tier` | str | `BEGINNER` / `INTERMEDIATE` / `ADVANCED` (always BEGINNER in the JSON tail) |
| `account_size_floor_usd` | float | Minimum account size at 2% sizing on 1 BEGINNER-tier contract |

Non-options alert groups (Daily Opportunity Brief etc.) leave these columns NULL - they're optional throughout the dispatcher pipeline.

## Wave Roadmap

Wave 1 ships the brief itself, the 6 feeders, and the pick journal. The remaining waves build on top:

| Wave | Scope | Status |
|------|-------|--------|
| **1** | Foundations: 6 ingestion scripts, 6 saved searches, OEB alert group, three-tier learner format, pick journal | **Shipped 2026-04-26** |
| **2** | Performance attribution: deterministic mark-to-market tracker, weekly Claude review, IMMUTABLE namespace, dual hit-rate, SPQL dashboard | **Shipped 2026-04-27** |
| **3** | Tier 2 signals: OI delta-day, 0DTE flow, gamma exposure (GEX) by strike | Pending |
| **4** | Tier 3 signals: calendar-spread screener, sweep detection, vol regime monitor | Pending |
| **5** | Paper-trading execution scaffolding: Alpaca / Tradier / IBKR order-ticket emitters, 30-day paper-only enforcement | Pending |

Wave 2 is the gate for the user's go-live decision: without mark-to-market on the journaled picks, "did the brief work?" is unanswerable.

## Wave 2 - Performance Attribution (shipped 2026-04-27)

Wave 2 ships the deterministic mark-to-market layer that makes the brief's hit rate measurable. Three components plus an architectural primitive:

### IMMUTABLE namespace

`indexes/IMMUTABLE/<subdir>/*.parquet` - a sibling tree under `indexes/` excluded from BOTH the standard cleanup and the logs cleanup. Future ingestion scripts can claim a subdir for any data that must survive forever (decade-horizon trading record). See [16_immutable_data_namespace.md](16_immutable_data_namespace.md). Wave 2 migrates the existing `ag_picks` journal from `indexes/logs/ag_picks/` to `indexes/IMMUTABLE/ag_picks/` automatically on engine startup.

### Deterministic pick tracker

A new ingestion script [oeb_pick_tracker_pro.json](../../script_library/scripts/oeb_pick_tracker_pro.json) runs daily at 21:30 UTC (~17:30 ET, post-close). For each open OEB pick:

1. Reads the pick's `option_legs_json` (Wave 1 leg-level metadata)
2. Fetches the current snapshot of every leg from Massive `/v3/snapshot/options/{ticker}/{contract}`
3. Computes a signed current net debit/credit
4. Applies the EXACT exit rules from the pick (`stop_loss_price`, `take_profit_price`, `suggested_sell_epoch`, contract expiration)
5. On a triggered exit, writes a closure event to `indexes/IMMUTABLE/ag_picks_closures/*.parquet` via `log_ag_pick_closure(...)`

Closure events carry: `outcome` (won / lost / time_exit / expired), `trigger_rule`, signed `entry_price` / `exit_price`, `pnl_per_contract_usd` (price math, not position math - stable across account scaling), `pnl_pct_vs_max_loss`, `closure_quality` (clean / illiquid / gap_through_stop / expired_otm / expired_itm), and the leg prices at close as JSON for forensic analysis.

**The tracker is hindsight-free by design.** Exit prices are evaluated against the rules-as-they-existed-at-entry. Re-judging "would I have closed earlier" is not allowed - that anti-pattern destroys the metric's trustworthiness. The risk-manager / examiner role is delegated to fixed deterministic rules; Claude only sees aggregated outcomes.

### Dual hit-rate computation

The user's account size grows over time. Some picks fit the account at entry; some don't. Two metrics:

- **`hit_rate_overall`** - every pick, regardless of fit
- **`hit_rate_account_fit`** - only picks where `account_size_floor_usd ≤ current_account_size_usd`

The `current_account_size_usd` setting (default `1000.0`) is the operator's configured current capital. Update it as the account grows; future picks recompute fit against the new value. Wave 2 closure rows carry both `fits_account_at_entry` and `fits_account_at_close` (today both compare to the current setting; full historical entry-time tracking is a Wave 3+ refinement).

### Weekly performance review (Claude interpretation layer)

A new alert group [options_performance_review.yaml](../../alert_groups/options_performance_review.yaml) runs Sunday 22 UTC (5pm ET). Reads three feeders:

- `oeb_perf_weekly` - closures in the past 7 days
- `oeb_perf_monthly` - closures in the past 30 days
- `oeb_perf_open_positions` - every OEB pick from the past 30 days

Asks Claude to play the **risk-manager / examiner** role (NOT the analyst - explicit anti-bias separation): aggregate the deterministic outcomes, identify best/worst signal classes, recommend ONE rule tweak for the upcoming week if (and only if) the data supports it.

#### Headline metric: account-fit hit rate

Two hit rates are computed every week, but the **headline** is `hit_rate_account_fit` - only counting closures where the pick fit the user's account size at entry (`fits_account_at_entry=true`). This is the metric that gates the operator's paper-trading-→-live decision at the configured account size; `hit_rate_overall` is reported as a secondary diagnostic. The Executive Summary always leads with the account-fit number.

#### Canonical signal-class labels

The review uses six canonical labels for `best_signal_class` / `worst_signal_class` so trend analysis on the persisted IMMUTABLE columns survives across weeks:

| Label | Source feeder | Edge it surfaces |
|-------|---------------|------------------|
| `iv_rank_high` | oeb_iv_rank (IVR ≥ 70) | SELL_PREMIUM setups |
| `iv_rank_low` | oeb_iv_rank (IVR ≤ 30) | BUY_PREMIUM setups |
| `term_backwardation` | oeb_term_structure | Front-month IV > back-month IV (event premium) |
| `skew_extreme` | oeb_skew_extreme | Stress-bid 25-delta skew (fear gauge) |
| `earnings_implied_move` | oeb_earnings_implied_move | Pre-earnings ATM IV crush opportunities |
| `unusual_flow` | oeb_unusual_activity | Vol/OI ratio spikes |

When a single pick lists multiple feeders in `source_signals` (semicolon-joined), the review attributes it to the first listed source - the dominant signal.

#### Calibration check

Closures over the past 30 days are bucketed by their pick's `conviction_pct` from the entry record (joined on `idea_id` from `oeb_perf_open_positions`):

| Bucket | Midpoint expected hit rate |
|--------|---------------------------|
| 75-79% | 77% |
| 80-84% | 82% |
| 85-89% | 87% |
| 90-94% | 92% |
| 95-100% | 97.5% |

For each bucket: count, hit rate, avg P&L per contract, Δ vs midpoint. The verdict is **well-calibrated** (every bucket's Δ within ±10pp), **overconfident** (avg Δ < -10pp - analyst's confidence outpaces actual hit rate), or **underconfident** (avg Δ > +10pp). When total closures over 30 days < 10, the section is replaced with "Insufficient data for calibration verdict (need ≥ 10 closures)" - a sample-size guard that prevents premature judgment.

The structured JSON tail is an OBJECT (not a picks array - different shape), parsed by a parallel dispatcher path `_extract_and_log_review_observations` that writes one summary row + N observation rows to `indexes/IMMUTABLE/ag_picks_review_observations/*.parquet`. The same data drives the email AND the SPQL-queryable observation log - dual delivery as the user requested.

### Performance dashboard SPQL templates

Ten templated queries in [05_cookbook.md](05_cookbook.md#options-edge-brief--performance-attribution-dashboard-wave-2) cover: overall + account-fit hit rate, P&L by signal class, hit rate by option structure, currently-open positions, days-held distribution, closure-quality audit, weekly hit-rate trend, latest rule-tweak recommendations, and the per-1-contract paper-trade scoreboard.

### What you should see day-to-day

1. The OEB dispatches twice daily (Wave 1) - picks land in `indexes/IMMUTABLE/ag_picks/`.
2. The tracker runs once daily after close (Wave 2) - closures land in `indexes/IMMUTABLE/ag_picks_closures/` for picks that hit an exit rule.
3. The weekly review runs Sunday evening (Wave 2) - summary + observations land in `indexes/IMMUTABLE/ag_picks_review_observations/`, plus an email to the configured recipient.
4. After ~30 days of data, run any cookbook query from the Search tab to see your hit rate, P&L distribution, and rule-tweak history.

## Configuration

The OEB inherits the global Claude settings (model, output token budget, timeout) but can be overridden per-AG via the YAML's advanced fields. Defaults:

| Setting | Value | Tunable via |
|---------|-------|-------------|
| `max_dispatches_per_day` | 2 | OEB YAML |
| `min_interval_between_runs_hours` | 4 | OEB YAML |
| `max_output_tokens` | 16384 | OEB YAML |
| `max_rows` (per feeder) | 100 | OEB YAML |
| Schedule cron | `30 14,19 * * 1-5` | OEB YAML |

## Triggering Manually

```
POST /api/alert-groups/options_edge_brief/run
```

This is the recommended way to validate the brief during PoC (with `email_address: ""` in the YAML). Once you trust the picks, add a recipient via the UI's Alert Groups → Edit dialog.

## Related Docs

- [12 - Alert Groups](12_alert_groups.md) - alert-group framework
- [11 - Claude Analyzer](11_claude_analyzer.md) - Claude API integration
- [14 - Logging](14_logging.md) - `ag_picks` and other Parquet log streams
- [09 - Ingestion Etiquette](09_ingestion_etiquette.md) - ingestion script conventions
